"""Vercel executors against the deterministic provider fake."""

from __future__ import annotations

import json
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_connectors.base import VerifyContext
from jhin_connectors.testing.fake_vercel import FakeVercelServer
from jhin_connectors.vercel.client import VercelApiError
from jhin_connectors.vercel.connector import VercelConnector
from jhin_connectors.vercel.schemas import (
    AliasAssignInput,
    DeploymentListInput,
    DeploymentListOutput,
    DeploymentLogsInput,
    DeploymentLogsOutput,
    DeploymentMutationOutput,
    DeploymentReadInput,
    EnvironmentMetadataInput,
    EnvironmentMetadataOutput,
    PreviewCreateInput,
    ProjectListInput,
    ProjectReadInput,
    PromoteInput,
    RedeployInput,
)
from jhin_connectors.vercel.tools import MAX_DEPLOYMENT_LIST_OUTPUT_BYTES, MAX_LOG_OUTPUT_BYTES
from jhin_db.models import Workspace
from jhin_secrets import SecretRedactor
from jhin_tools.builtin import ToolExecutionContext
from jhin_tools.sanitize import MAX_DOCUMENT_BYTES, sanitize_payload

EXECUTORS = {definition.name: executor for definition, executor in VercelConnector().tools()}


@pytest.fixture
def fake_vercel(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeVercelServer]:
    with FakeVercelServer() as server:
        monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", server.base_url)
        yield server


@pytest.fixture
async def vercel_connection(  # type: ignore[no-untyped-def]
    workspace: Workspace, make_connection, fake_vercel: FakeVercelServer
):
    return await make_connection(
        workspace,
        connector_type="vercel",
        auth_type="access_token",
        credentials={"token": fake_vercel.state.token},
        config={"base_url": fake_vercel.base_url, "team_id": "team_seeded"},
    )


async def _run(
    name: str,
    context: ToolExecutionContext,
    payload: BaseModel,
) -> BaseModel:
    return await EXECUTORS[name](context, payload)


async def test_verify_connection_and_project_reads_are_display_safe(
    fake_vercel: FakeVercelServer,
) -> None:
    health = await VercelConnector().verify_connection(
        VerifyContext(
            auth_type="access_token",
            credentials={"token": fake_vercel.state.token},
            config={"base_url": fake_vercel.base_url, "team_id": "team_seeded"},
        )
    )

    assert health.ok
    assert health.details == {
        "username": "fake-user",
        "email": "fake@example.test",
        "team_id": "team_seeded",
    }
    assert fake_vercel.state.token not in health.model_dump_json()


async def test_verify_connection_rejects_auth_and_provider_errors_safely(
    fake_vercel: FakeVercelServer,
) -> None:
    connector = VercelConnector()
    unsupported = await connector.verify_connection(
        VerifyContext(auth_type="oauth", credentials={}, config={})
    )
    missing = await connector.verify_connection(
        VerifyContext(auth_type="access_token", credentials={}, config={})
    )
    invalid = await connector.verify_connection(
        VerifyContext(
            auth_type="access_token",
            credentials={"token": "wrong-token-marker"},
            config={"base_url": fake_vercel.base_url},
        )
    )

    assert not unsupported.ok
    assert not missing.ok
    assert not invalid.ok
    assert "wrong-token-marker" not in invalid.message


def test_validate_settings_normalizes_origin_and_rejects_unsafe_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = VercelConnector()
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", "http://fake-vercel:8080")

    assert connector.validate_settings(
        "access_token",
        {"base_url": "HTTP://FAKE-VERCEL:8080/", "team_id": "team_123"},
    ) == {"base_url": "http://fake-vercel:8080", "team_id": "team_123"}
    marker = "unsafe-vercel-origin-marker"
    with pytest.raises(ValueError) as exc_info:
        connector.validate_settings(
            "access_token", {"base_url": f"https://{marker}@api.vercel.com"}
        )
    assert marker not in str(exc_info.value)


@pytest.mark.parametrize(
    "team_id",
    ["", "team id", "../team", "team_" + "x" * 200],
)
def test_validate_settings_rejects_invalid_team_id(team_id: str) -> None:
    with pytest.raises(ValueError, match="team_id"):
        VercelConnector().validate_settings(
            "access_token",
            {"base_url": "https://api.vercel.com", "team_id": team_id},
        )


@pytest.mark.parametrize(
    "alias",
    ["-bad.example", "bad-.example", "bad..example", ".bad.example", "bad.example."],
)
def test_alias_validation_rejects_invalid_dns_labels(alias: str) -> None:
    with pytest.raises(ValidationError):
        AliasAssignInput(
            connection_id="connection",
            project_id="project",
            deployment_id="deployment",
            alias=alias,
        )


async def test_project_list_and_read_filter_unknown_provider_fields(
    context: ToolExecutionContext, vercel_connection, fake_vercel: FakeVercelServer
) -> None:
    listed = await _run(
        "vercel.project.list",
        context,
        ProjectListInput(connection_id=str(vercel_connection.id), limit=20),
    )
    read = await _run(
        "vercel.project.read",
        context,
        ProjectReadInput(connection_id=str(vercel_connection.id), project_id="prj_github"),
    )

    rendered = listed.model_dump_json() + read.model_dump_json()
    assert "prj_github" in rendered
    assert "provider-project-secret" not in rendered
    assert fake_vercel.state.token not in rendered


async def test_foreign_workspace_connection_fails_safely_before_provider_request(
    context: ToolExecutionContext,
    session: AsyncSession,
    make_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    foreign_workspace = Workspace(name="Foreign", slug="foreign-vercel-workspace")
    session.add(foreign_workspace)
    await session.flush()
    foreign_connection = await make_connection(
        foreign_workspace,
        connector_type="vercel",
        auth_type="access_token",
        credentials={"token": fake_vercel.state.token},
        config={"base_url": fake_vercel.base_url},
    )

    with pytest.raises(VercelApiError) as exc_info:
        await _run(
            "vercel.project.list",
            context,
            ProjectListInput(connection_id=str(foreign_connection.id)),
        )

    assert exc_info.value.code == "connection_unavailable"
    assert exc_info.value.side_effect_possible is False
    assert not fake_vercel.state.requests_for("GET", "/v9/projects")


async def test_project_list_is_capped_and_reports_truncation(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    fake_vercel.state.seed_many_projects(240)

    output = await _run(
        "vercel.project.list",
        context,
        ProjectListInput(connection_id=str(vercel_connection.id), limit=200),
    )

    assert len(output.projects) == 200  # type: ignore[attr-defined]
    assert output.truncated is True  # type: ignore[attr-defined]
    assert len(fake_vercel.state.requests_for("GET", "/v9/projects")) <= 5


async def test_malformed_provider_shape_maps_to_safe_stable_error(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    marker = "provider-value-must-not-enter-error"
    fake_vercel.state.projects["prj_github"]["createdAt"] = marker

    with pytest.raises(VercelApiError) as exc_info:
        await _run(
            "vercel.project.read",
            context,
            ProjectReadInput(connection_id=str(vercel_connection.id), project_id="prj_github"),
        )

    assert exc_info.value.code == "invalid_provider_response"
    assert marker not in str(exc_info.value)


async def test_corrupt_project_response_id_blocks_project_actions_and_all_mutations(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    fake_vercel.state.projects["prj_github"]["id"] = "prj_wrong"
    calls: list[tuple[str, BaseModel]] = [
        (
            "vercel.project.read",
            ProjectReadInput(connection_id=str(vercel_connection.id), project_id="prj_github"),
        ),
        (
            "vercel.environment_metadata.read",
            EnvironmentMetadataInput(
                connection_id=str(vercel_connection.id), project_id="prj_github"
            ),
        ),
        (
            "vercel.deployment.list",
            DeploymentListInput(connection_id=str(vercel_connection.id), project_id="prj_github"),
        ),
        (
            "vercel.deployment.read",
            DeploymentReadInput(
                connection_id=str(vercel_connection.id),
                project_id="prj_github",
                deployment_id="dpl_preview",
            ),
        ),
        (
            "vercel.deployment.logs.read",
            DeploymentLogsInput(
                connection_id=str(vercel_connection.id),
                project_id="prj_github",
                deployment_id="dpl_preview",
            ),
        ),
        (
            "vercel.deployment.preview.create",
            PreviewCreateInput(
                connection_id=str(vercel_connection.id),
                project_id="prj_github",
                git_provider="github",
                repository_id="101",
                ref="feature/safe",
            ),
        ),
        (
            "vercel.deployment.redeploy",
            RedeployInput(
                connection_id=str(vercel_connection.id),
                project_id="prj_github",
                deployment_id="dpl_preview",
                environment="preview",
            ),
        ),
        (
            "vercel.deployment.promote",
            PromoteInput(
                connection_id=str(vercel_connection.id),
                project_id="prj_github",
                deployment_id="dpl_preview",
            ),
        ),
        (
            "vercel.deployment.alias.assign",
            AliasAssignInput(
                connection_id=str(vercel_connection.id),
                project_id="prj_github",
                deployment_id="dpl_production",
                alias="www.example.test",
            ),
        ),
    ]

    for name, payload in calls:
        with pytest.raises(VercelApiError) as exc_info:
            await _run(name, context, payload)
        assert exc_info.value.code == "project_scope_mismatch", name
        assert exc_info.value.side_effect_possible is False, name

    assert fake_vercel.state.snapshot()["counters"] == {
        "preview_create": 0,
        "redeploy": 0,
        "promote": 0,
        "alias": 0,
    }


async def test_environment_metadata_never_returns_provider_value(
    context: ToolExecutionContext, vercel_connection
) -> None:
    output = await _run(
        "vercel.environment_metadata.read",
        context,
        EnvironmentMetadataInput(connection_id=str(vercel_connection.id), project_id="prj_github"),
    )

    assert isinstance(output, EnvironmentMetadataOutput)
    assert output.variables[0].key == "DATABASE_URL"
    rendered = output.model_dump_json()
    for forbidden in (
        "must-never-leak",
        "encrypted-must-never-leak",
        "internal-must-never-leak",
        "unknown-provider-secret",
    ):
        assert forbidden not in rendered


async def test_environment_metadata_bounds_provider_strings_and_lists(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    fake_vercel.state.env_records["prj_github"] = [
        {
            "id": "env-" + "x" * 600,
            "key": "K" * 600,
            "target": ["preview"] * 12,
            "type": "encrypted-" + "x" * 100,
            "createdAt": 1,
            "updatedAt": 2,
            "gitBranch": "b" * 600,
            "value": "must-never-leak",
        }
    ] * 205

    output = await _run(
        "vercel.environment_metadata.read",
        context,
        EnvironmentMetadataInput(connection_id=str(vercel_connection.id), project_id="prj_github"),
    )

    assert isinstance(output, EnvironmentMetadataOutput)
    assert output.truncated
    assert len(output.variables) <= 200
    first = output.variables[0]
    assert len(first.environment_id) <= 200
    assert len(first.key) <= 256
    assert len(first.targets) <= 10
    assert len(first.git_branch) <= 250


def test_bounded_output_models_reject_unbounded_direct_construction() -> None:
    from jhin_connectors.vercel.schemas import DeploymentLogEvent, EnvironmentVariableMetadata

    with pytest.raises(ValidationError):
        DeploymentLogEvent(timestamp=1, message="x" * 4_001)
    with pytest.raises(ValidationError):
        DeploymentLogEvent(timestamp=2**63, message="bounded")
    with pytest.raises(ValidationError):
        EnvironmentVariableMetadata(key="x" * 257)
    with pytest.raises(ValidationError):
        EnvironmentMetadataOutput(
            variables=[EnvironmentVariableMetadata(key=f"KEY_{index}") for index in range(201)]
        )


async def test_logs_are_time_count_and_output_byte_bounded(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    output = await _run(
        "vercel.deployment.logs.read",
        context,
        DeploymentLogsInput(
            connection_id=str(vercel_connection.id),
            project_id="prj_github",
            deployment_id="dpl_preview",
            limit=200,
        ),
    )

    assert isinstance(output, DeploymentLogsOutput)
    assert len(output.events) <= 200
    assert len(output.model_dump_json().encode()) <= MAX_LOG_OUTPUT_BYTES
    sanitized = sanitize_payload(output.model_dump(mode="json"))
    assert "original_size_bytes" not in sanitized
    assert len(json.dumps(sanitized, sort_keys=True).encode()) < 32_768
    request = fake_vercel.state.requests_for("GET", "/v3/deployments/dpl_preview/events")[-1]
    assert int(request["query"]["limit"][0]) <= 200
    assert int(request["query"]["until"][0]) - int(request["query"]["since"][0]) <= 86_400_000
    assert request["query"]["follow"] == ["0"]


def test_log_input_rejects_windows_wider_than_24_hours() -> None:
    with pytest.raises(ValidationError, match="24 hours"):
        DeploymentLogsInput(
            connection_id="connection",
            project_id="project",
            deployment_id="deployment",
            since=1,
            until=86_400_002,
        )

    with pytest.raises(ValidationError):
        DeploymentLogsInput(
            connection_id="connection",
            project_id="project",
            deployment_id="deployment",
            since=2**63,
        )


async def test_one_sided_log_window_stays_inside_signed_timestamp_bound(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    await _run(
        "vercel.deployment.logs.read",
        context,
        DeploymentLogsInput(
            connection_id=str(vercel_connection.id),
            project_id="prj_github",
            deployment_id="dpl_preview",
            since=2**63 - 10,
            limit=1,
        ),
    )

    request = fake_vercel.state.requests_for("GET", "/v3/deployments/dpl_preview/events")[-1]
    assert int(request["query"]["until"][0]) == 2**63 - 1


async def test_logs_stop_at_input_limit_when_provider_ignores_limit(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    fake_vercel.state.ignore_event_limit = True

    output = await _run(
        "vercel.deployment.logs.read",
        context,
        DeploymentLogsInput(
            connection_id=str(vercel_connection.id),
            project_id="prj_github",
            deployment_id="dpl_preview",
            limit=10,
        ),
    )

    assert len(output.events) == 10  # type: ignore[attr-defined]
    assert output.truncated is True  # type: ignore[attr-defined]


async def test_deployment_list_always_sends_project_id_and_has_bounded_pagination(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    async with httpx.AsyncClient() as client:
        scenario = await client.post(
            f"{fake_vercel.base_url}/_scenario",
            json={"scenario": "deployment_list_pagination"},
            timeout=5,
        )
    assert scenario.status_code == 200
    assert scenario.json() == {"armed": "deployment_list_pagination"}

    output = await _run(
        "vercel.deployment.list",
        context,
        DeploymentListInput(
            connection_id=str(vercel_connection.id), project_id="prj_github", limit=200
        ),
    )

    assert isinstance(output, DeploymentListOutput)
    assert len(output.deployments) == 200
    assert output.truncated
    sanitized = sanitize_payload(output.model_dump(mode="json"))
    assert "original_size_bytes" not in sanitized
    assert len(sanitized["deployments"]) == 200
    requests = fake_vercel.state.requests_for("GET", "/v6/deployments")
    assert 1 < len(requests) <= 5
    assert all(request["query"]["projectId"] == ["prj_github"] for request in requests)
    assert all(int(request["query"]["limit"][0]) <= 100 for request in requests)


async def test_deployment_list_survives_gateway_cap_after_redaction_expansion(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with httpx.AsyncClient() as client:
        scenario = await client.post(
            f"{fake_vercel.base_url}/_scenario",
            json={"scenario": "deployment_list_pagination"},
            timeout=5,
        )
    assert scenario.status_code == 200
    redactor = SecretRedactor()
    redactor.register("preview")
    redactor.register("dpl_bu")
    redactor.register("prj_gi")
    monkeypatch.setattr("jhin_tools.sanitize.get_redactor", lambda: redactor)

    output = await _run(
        "vercel.deployment.list",
        context,
        DeploymentListInput(
            connection_id=str(vercel_connection.id), project_id="prj_github", limit=200
        ),
    )

    sanitized = sanitize_payload(output.model_dump(mode="json"), redactor=redactor)

    assert "original_size_bytes" not in sanitized
    assert 0 < len(sanitized["deployments"]) < 200
    assert sanitized["truncated"] is True
    assert {row["target"] for row in sanitized["deployments"]} == {"[REDACTED]"}
    assert all("[REDACTED]" in row["deployment_id"] for row in sanitized["deployments"])
    serialized_bytes = len(json.dumps(sanitized, ensure_ascii=False).encode())
    assert serialized_bytes <= MAX_DEPLOYMENT_LIST_OUTPUT_BYTES < MAX_DOCUMENT_BYTES


async def test_deployment_list_rejects_any_mixed_project_page_without_returning_rows(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    async with httpx.AsyncClient() as client:
        scenario = await client.post(
            f"{fake_vercel.base_url}/_scenario",
            json={"scenario": "deployment_list_mixed_project"},
            timeout=5,
        )
    assert scenario.status_code == 200
    assert scenario.json() == {"armed": "deployment_list_mixed_project"}

    with pytest.raises(VercelApiError) as exc_info:
        await _run(
            "vercel.deployment.list",
            context,
            DeploymentListInput(
                connection_id=str(vercel_connection.id), project_id="prj_github", limit=20
            ),
        )

    assert exc_info.value.code == "project_scope_mismatch"
    assert exc_info.value.side_effect_possible is False


def test_fake_scenarios_are_closed_and_reset_restores_all_state(
    fake_vercel: FakeVercelServer,
) -> None:
    state = fake_vercel.state
    token = state.token
    seeded_projects = deepcopy(state.projects)
    seeded_deployments = deepcopy(state.deployments)
    seeded_env_records = deepcopy(state.env_records)
    seeded_events = deepcopy(state.events)

    pagination = httpx.post(
        f"{fake_vercel.base_url}/_scenario",
        json={"scenario": "deployment_list_pagination"},
        timeout=5,
    )
    mixed = httpx.post(
        f"{fake_vercel.base_url}/_scenario",
        json={"scenario": "deployment_list_mixed_project"},
        timeout=5,
    )
    arbitrary = httpx.post(
        f"{fake_vercel.base_url}/_scenario",
        json={"scenario": "deployment_list_pagination", "count": 10_000},
        timeout=5,
    )
    unknown = httpx.post(
        f"{fake_vercel.base_url}/_scenario",
        json={"scenario": "arbitrary_resource_mutation"},
        timeout=5,
    )

    assert pagination.json() == {"armed": "deployment_list_pagination"}
    assert mixed.json() == {"armed": "deployment_list_mixed_project"}
    assert arbitrary.status_code == 400
    assert arbitrary.json() == {"error": "unknown scenario"}
    assert unknown.status_code == 400
    assert unknown.json() == {"error": "unknown scenario"}

    state.projects["prj_github"]["name"] = "mutated-project"
    state.deployments["dpl_preview"]["target"] = "production"
    state.env_records["prj_github"].clear()
    state.events["dpl_preview"].clear()
    state.record_mutation(
        "alias",
        {
            "method": "POST",
            "path": "/v2/deployments/dpl_production/aliases",
            "query": {},
            "body": {"alias": "mutated.example.test"},
        },
    )
    state.arm_fault("redeploy")
    state.redirects["/v2/user"] = "https://redirect.invalid"
    state.repeat_pagination_cursor = True
    state.ignore_event_limit = True
    state.webhook_counter = 9
    authenticated = httpx.get(
        f"{fake_vercel.base_url}/v2/user",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    assert authenticated.status_code == 302

    before_reset = httpx.get(f"{fake_vercel.base_url}/_state", timeout=5)
    reset = httpx.post(f"{fake_vercel.base_url}/_reset", timeout=5)
    after_reset = httpx.get(f"{fake_vercel.base_url}/_state", timeout=5)

    assert before_reset.json()["scenarios"] == [
        "deployment_list_mixed_project",
        "deployment_list_pagination",
    ]
    assert token not in before_reset.text
    assert reset.status_code == 200
    assert reset.json() == {"ok": True}
    assert state.token == token
    assert state.projects == seeded_projects
    assert state.deployments == seeded_deployments
    assert state.env_records == seeded_env_records
    assert state.events == seeded_events
    assert state.faults == set()
    assert state.redirects == {}
    assert state.mixed_project_list_row is False
    assert state.repeat_pagination_cursor is False
    assert state.ignore_event_limit is False
    assert state.webhook_counter == 0
    assert after_reset.json() == {
        "counters": {"preview_create": 0, "redeploy": 0, "promote": 0, "alias": 0},
        "last_requests": {},
        "requests": [],
        "scenarios": [],
    }
    assert token not in reset.text + after_reset.text


async def test_deployment_list_rejects_repeated_pagination_cursor(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    fake_vercel.state.seed_many_deployments("prj_github", 240)
    fake_vercel.state.repeat_pagination_cursor = True

    with pytest.raises(VercelApiError) as exc_info:
        await _run(
            "vercel.deployment.list",
            context,
            DeploymentListInput(
                connection_id=str(vercel_connection.id), project_id="prj_github", limit=200
            ),
        )

    assert exc_info.value.code == "invalid_pagination"
    assert len(fake_vercel.state.requests_for("GET", "/v6/deployments")) <= 5


async def test_deployment_project_mismatch_has_zero_side_effects(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    before = fake_vercel.state.snapshot()["counters"]
    payloads: list[tuple[str, BaseModel]] = [
        (
            "vercel.deployment.redeploy",
            RedeployInput(
                connection_id=str(vercel_connection.id),
                project_id="prj_github",
                deployment_id="dpl_other",
                environment="preview",
            ),
        ),
        (
            "vercel.deployment.promote",
            PromoteInput(
                connection_id=str(vercel_connection.id),
                project_id="prj_github",
                deployment_id="dpl_other",
            ),
        ),
        (
            "vercel.deployment.alias.assign",
            AliasAssignInput(
                connection_id=str(vercel_connection.id),
                project_id="prj_github",
                deployment_id="dpl_other",
                alias="www.example.test",
            ),
        ),
    ]

    for name, payload in payloads:
        with pytest.raises(VercelApiError) as exc_info:
            await _run(name, context, payload)
        assert exc_info.value.code == "project_scope_mismatch"
        assert exc_info.value.side_effect_possible is False

    assert fake_vercel.state.snapshot()["counters"] == before


async def test_redeploy_rejects_environment_mismatch_before_side_effect(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    with pytest.raises(VercelApiError) as exc_info:
        await _run(
            "vercel.deployment.redeploy",
            context,
            RedeployInput(
                connection_id=str(vercel_connection.id),
                project_id="prj_github",
                deployment_id="dpl_production",
                environment="preview",
            ),
        )

    assert exc_info.value.code == "environment_scope_mismatch"
    assert exc_info.value.side_effect_possible is False
    assert fake_vercel.state.snapshot()["counters"]["redeploy"] == 0


@pytest.mark.parametrize(
    ("project_id", "provider", "repository_id", "expected_git_source"),
    [
        (
            "prj_github",
            "github",
            "101",
            {"type": "github", "ref": "feature/safe", "repoId": 101},
        ),
        (
            "prj_gitlab",
            "gitlab",
            "gl-project-202",
            {"type": "gitlab", "ref": "feature/safe", "projectId": "gl-project-202"},
        ),
        (
            "prj_bitbucket",
            "bitbucket",
            "{bb-repo-303}",
            {
                "type": "bitbucket",
                "ref": "feature/safe",
                "repoUuid": "{bb-repo-303}",
                "workspaceUuid": "{bb-workspace}",
            },
        ),
    ],
)
async def test_preview_create_uses_exact_fetched_git_link_contract(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
    project_id: str,
    provider: str,
    repository_id: str,
    expected_git_source: dict[str, object],
) -> None:
    output = await _run(
        "vercel.deployment.preview.create",
        replace(context, tool_call_id=context.run_id),
        PreviewCreateInput(
            connection_id=str(vercel_connection.id),
            project_id=project_id,
            git_provider=provider,
            repository_id=repository_id,
            ref="feature/safe",
        ),
    )

    assert isinstance(output, DeploymentMutationOutput)
    request = fake_vercel.state.snapshot()["last_requests"]["preview_create"]
    assert request["body"]["target"] == "preview"
    assert request["body"]["project"] == project_id
    assert request["body"]["gitSource"] == expected_git_source


@pytest.mark.parametrize(
    ("project_id", "provider", "repository_id"),
    [
        ("prj_unlinked", "github", "101"),
        ("prj_unknown_link", "github", "101"),
        ("prj_github_custom", "github", "101"),
        ("prj_github", "gitlab", "101"),
        ("prj_github", "github", "999"),
    ],
)
async def test_preview_create_rejects_unlinked_or_mismatched_git_repository_before_side_effect(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
    project_id: str,
    provider: str,
    repository_id: str,
) -> None:
    with pytest.raises(VercelApiError) as exc_info:
        await _run(
            "vercel.deployment.preview.create",
            context,
            PreviewCreateInput(
                connection_id=str(vercel_connection.id),
                project_id=project_id,
                git_provider=provider,
                repository_id=repository_id,
                ref="feature/safe",
            ),
        )

    assert exc_info.value.code == "repository_scope_mismatch"
    assert exc_info.value.side_effect_possible is False
    assert fake_vercel.state.snapshot()["counters"]["preview_create"] == 0


async def test_github_repo_id_is_bounded_before_display_or_mutation(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    fake_vercel.state.projects["prj_github"]["link"]["repoId"] = 2**63

    project = await _run(
        "vercel.project.read",
        context,
        ProjectReadInput(connection_id=str(vercel_connection.id), project_id="prj_github"),
    )
    assert project.repository_id == ""  # type: ignore[attr-defined]

    with pytest.raises(VercelApiError) as exc_info:
        await _run(
            "vercel.deployment.preview.create",
            context,
            PreviewCreateInput(
                connection_id=str(vercel_connection.id),
                project_id="prj_github",
                git_provider="github",
                repository_id="101",
                ref="feature/safe",
            ),
        )
    assert exc_info.value.code == "repository_scope_mismatch"
    assert exc_info.value.side_effect_possible is False
    assert fake_vercel.state.snapshot()["counters"]["preview_create"] == 0


async def test_redeploy_payload_matches_current_vercel_contract(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    output = await _run(
        "vercel.deployment.redeploy",
        replace(context, tool_call_id=context.run_id),
        RedeployInput(
            connection_id=str(vercel_connection.id),
            project_id="prj_github",
            deployment_id="dpl_preview",
            environment="preview",
        ),
    )

    assert isinstance(output, DeploymentMutationOutput)
    request = fake_vercel.state.snapshot()["last_requests"]["redeploy"]
    assert request["query"]["forceNew"] == ["1"]
    assert request["body"] == {
        "deploymentId": "dpl_preview",
        "name": "github-project",
        "target": "preview",
        "meta": {"action": "redeploy"},
    }


async def test_promote_and_alias_verify_project_ownership_first(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    promoted = await _run(
        "vercel.deployment.promote",
        context,
        PromoteInput(
            connection_id=str(vercel_connection.id),
            project_id="prj_github",
            deployment_id="dpl_preview",
        ),
    )
    aliased = await _run(
        "vercel.deployment.alias.assign",
        context,
        AliasAssignInput(
            connection_id=str(vercel_connection.id),
            project_id="prj_github",
            deployment_id="dpl_production",
            alias="www.example.test",
        ),
    )

    assert isinstance(promoted, DeploymentMutationOutput)
    assert aliased.model_dump() == {
        "deployment_id": "dpl_production",
        "project_id": "prj_github",
        "alias": "www.example.test",
        "assigned": True,
    }
    snapshot = fake_vercel.state.snapshot()
    assert snapshot["last_requests"]["promote"]["body"] == {}
    assert snapshot["last_requests"]["alias"]["body"] == {"alias": "www.example.test"}


async def test_team_id_is_sent_without_entering_outputs(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    output = await _run(
        "vercel.project.read",
        context,
        ProjectReadInput(connection_id=str(vercel_connection.id), project_id="prj_github"),
    )

    request = fake_vercel.state.requests_for("GET", "/v9/projects/prj_github")[-1]
    assert request["query"]["teamId"] == ["team_seeded"]
    assert "team_seeded" not in output.model_dump_json()


async def test_no_undocumented_idempotency_field_is_sent(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    await _run(
        "vercel.deployment.redeploy",
        replace(context, tool_call_id=context.run_id),
        RedeployInput(
            connection_id=str(vercel_connection.id),
            project_id="prj_github",
            deployment_id="dpl_preview",
            environment="preview",
        ),
    )

    request = fake_vercel.state.snapshot()["last_requests"]["redeploy"]
    rendered = json.dumps(request).lower()
    assert str(context.run_id) not in rendered
    assert "idempotency" not in rendered
    assert "tool_call" not in rendered


@pytest.mark.parametrize("action", ["preview_create", "redeploy", "promote", "alias"])
async def test_post_effect_fault_is_one_shot_and_visible_in_state(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
    action: str,
) -> None:
    payloads: dict[str, tuple[str, BaseModel]] = {
        "preview_create": (
            "vercel.deployment.preview.create",
            PreviewCreateInput(
                connection_id=str(vercel_connection.id),
                project_id="prj_github",
                git_provider="github",
                repository_id="101",
                ref="feature/fault",
            ),
        ),
        "redeploy": (
            "vercel.deployment.redeploy",
            RedeployInput(
                connection_id=str(vercel_connection.id),
                project_id="prj_github",
                deployment_id="dpl_preview",
                environment="preview",
            ),
        ),
        "promote": (
            "vercel.deployment.promote",
            PromoteInput(
                connection_id=str(vercel_connection.id),
                project_id="prj_github",
                deployment_id="dpl_preview",
            ),
        ),
        "alias": (
            "vercel.deployment.alias.assign",
            AliasAssignInput(
                connection_id=str(vercel_connection.id),
                project_id="prj_github",
                deployment_id="dpl_production",
                alias="fault.example.test",
            ),
        ),
    }
    name, payload = payloads[action]
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{fake_vercel.base_url}/_fault",
            json={"mutation": action},
        )
        assert response.status_code == 200

    with pytest.raises(VercelApiError) as exc_info:
        await _run(name, context, payload)
    assert exc_info.value.code == "provider_transport_error"
    assert exc_info.value.side_effect_possible is True
    assert fake_vercel.state.snapshot()["counters"][action] == 1

    await _run(name, context, payload)
    assert fake_vercel.state.snapshot()["counters"][action] == 2


async def test_provider_redirect_to_unapproved_origin_is_rejected(
    context: ToolExecutionContext,
    vercel_connection,
    fake_vercel: FakeVercelServer,
) -> None:
    fake_vercel.state.redirects["/v9/projects/prj_github"] = (
        "https://redirect-should-never-run.invalid/provider-secret"
    )

    with pytest.raises(VercelApiError) as exc_info:
        await _run(
            "vercel.project.read",
            context,
            ProjectReadInput(connection_id=str(vercel_connection.id), project_id="prj_github"),
        )

    assert exc_info.value.code == "provider_redirect"
    assert "redirect-should-never-run" not in str(exc_info.value)


async def test_legacy_stored_unapproved_origin_is_revalidated_at_execution(
    context: ToolExecutionContext,
    workspace: Workspace,
    make_connection,
) -> None:
    marker = "legacy-unapproved-vercel-marker.invalid"
    connection = await make_connection(
        workspace,
        connector_type="vercel",
        auth_type="access_token",
        credentials={"token": "safe-token"},
        config={"base_url": f"https://{marker}"},
    )

    with pytest.raises(VercelApiError) as exc_info:
        await _run(
            "vercel.project.list",
            context,
            ProjectListInput(connection_id=str(connection.id)),
        )

    assert exc_info.value.code == "endpoint_not_allowed"
    assert marker not in str(exc_info.value)
