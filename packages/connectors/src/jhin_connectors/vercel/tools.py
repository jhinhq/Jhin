"""Scoped Vercel tool definitions and bounded provider executors."""

from __future__ import annotations

import json
import time
from typing import Any, cast

from pydantic import BaseModel

from jhin_connectors.execution import ConnectionResolutionError, resolve_connection
from jhin_connectors.vercel.client import (
    DEFAULT_BASE_URL,
    VercelApiError,
    VercelClient,
    deployment_project_id,
    validate_team_id,
)
from jhin_connectors.vercel.schemas import (
    AliasAssignInput,
    AliasAssignOutput,
    DeploymentListInput,
    DeploymentListItem,
    DeploymentListOutput,
    DeploymentLogEvent,
    DeploymentLogsInput,
    DeploymentLogsOutput,
    DeploymentMutationOutput,
    DeploymentReadInput,
    DeploymentReadOutput,
    EnvironmentMetadataInput,
    EnvironmentMetadataOutput,
    EnvironmentVariableMetadata,
    PreviewCreateInput,
    ProjectListInput,
    ProjectListOutput,
    ProjectReadInput,
    ProjectReadOutput,
    PromoteInput,
    RedeployInput,
)
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor
from jhin_tools.sanitize import sanitize_payload

MAX_DEPLOYMENT_LIST_OUTPUT_BYTES = 28_000
MAX_LOG_OUTPUT_BYTES = 28_000
_MAX_LOG_MESSAGE_CHARS = 4_000
_MAX_ENV_ROWS = 200
_LOG_WINDOW_MS = 86_400_000


def _provider_shape(message: str) -> VercelApiError:
    return VercelApiError(message, code="invalid_provider_response")


def _gateway_document_retained(output: BaseModel, *, maximum: int) -> bool:
    dumped = output.model_dump(mode="json")
    encoded = json.dumps(dumped, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) > maximum:
        return False
    sanitized = sanitize_payload(
        dumped,
        max_document_bytes=maximum,
    )
    return "original_size_bytes" not in sanitized


def _required_string(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _provider_shape(f"Vercel returned an invalid {field}")
    return value


def _display_string(value: Any, *, field: str, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _provider_shape(f"Vercel returned an invalid {field}")
    return value[:maximum]


def _timestamp(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 2**63 - 1:
        raise _provider_shape(f"Vercel returned an invalid {field}")
    return value


def _provider_state(deployment: dict[str, Any]) -> str:
    value = deployment.get("readyState", deployment.get("state"))
    return _display_string(value, field="deployment state", maximum=50)


def _deployment_id(deployment: dict[str, Any]) -> str:
    uid = deployment.get("uid")
    identifier = deployment.get("id")
    values = [candidate for candidate in (uid, identifier) if candidate is not None]
    if not values:
        raise _provider_shape("Vercel returned a deployment without an id")
    parsed = [
        _required_string(candidate, field="deployment id", maximum=200) for candidate in values
    ]
    if any(candidate != parsed[0] for candidate in parsed[1:]):
        raise _provider_shape("Vercel returned inconsistent deployment ids")
    return parsed[0]


def _recognized_link(project: dict[str, Any]) -> tuple[str, str]:
    link = project.get("link")
    if not isinstance(link, dict):
        return "", ""
    link_type = link.get("type")
    if link_type in {"github", "github-limited"}:
        repo_id = link.get("repoId")
        if (
            isinstance(repo_id, bool)
            or not isinstance(repo_id, int)
            or not 1 <= repo_id <= 2**63 - 1
        ):
            return "", ""
        return "github", str(repo_id)
    if link_type == "gitlab":
        project_id = link.get("projectId")
        if isinstance(project_id, str) and 0 < len(project_id) <= 200:
            return "gitlab", project_id
        return "", ""
    if link_type == "bitbucket":
        repo_uuid = link.get("uuid")
        if isinstance(repo_uuid, str) and 0 < len(repo_uuid) <= 200:
            return "bitbucket", repo_uuid
        return "", ""
    return "", ""


def _project_info(project: dict[str, Any]) -> ProjectReadOutput:
    project_id = _required_string(project.get("id"), field="project id", maximum=200)
    provider, repository_id = _recognized_link(project)
    return ProjectReadOutput(
        project_id=project_id,
        name=_required_string(project.get("name"), field="project name", maximum=256),
        framework=_display_string(project.get("framework"), field="framework", maximum=100),
        created_at=_timestamp(project.get("createdAt"), field="project creation timestamp"),
        updated_at=_timestamp(project.get("updatedAt"), field="project update timestamp"),
        git_provider=provider,
        repository_id=repository_id,
    )


def _deployment_info(deployment: dict[str, Any]) -> DeploymentReadOutput:
    return DeploymentReadOutput(
        deployment_id=_deployment_id(deployment),
        project_id=deployment_project_id(deployment),
        name=_required_string(deployment.get("name"), field="deployment name", maximum=256),
        url=_display_string(deployment.get("url"), field="deployment URL", maximum=2_048),
        state=_provider_state(deployment),
        target=_display_string(deployment.get("target"), field="deployment target", maximum=50),
        created_at=_timestamp(deployment.get("created"), field="deployment creation timestamp"),
        ready_at=_timestamp(deployment.get("ready"), field="deployment ready timestamp"),
        inspector_url=_display_string(
            deployment.get("inspectorUrl"), field="deployment inspector URL", maximum=2_048
        ),
    )


def _deployment_list_item(deployment: dict[str, Any]) -> DeploymentListItem:
    return DeploymentListItem(
        deployment_id=_deployment_id(deployment),
        project_id=deployment_project_id(deployment),
        state=_provider_state(deployment),
        target=_display_string(deployment.get("target"), field="deployment target", maximum=50),
        created_at=_timestamp(deployment.get("created"), field="deployment creation timestamp"),
    )


async def _api(ctx: ToolExecutionContext, connection_id: str) -> VercelClient:
    try:
        resolved = await resolve_connection(ctx, connection_id, connector_type="vercel")
    except ConnectionResolutionError:
        raise VercelApiError(
            "Vercel connection is unavailable",
            code="connection_unavailable",
            side_effect_possible=False,
        ) from None
    if resolved.connection.auth_type != "access_token":
        raise VercelApiError(
            "This Vercel connection does not use access-token authentication",
            code="unsupported_auth_type",
            side_effect_possible=False,
        )
    token = resolved.credentials.get("token")
    if not isinstance(token, str) or not token:
        raise VercelApiError(
            "This Vercel connection has no access token",
            code="credential_invalid",
            side_effect_possible=False,
        )
    config = resolved.config
    base_url = config.get("base_url", DEFAULT_BASE_URL)
    if not isinstance(base_url, str):
        raise VercelApiError(
            "Vercel API target is not allowed",
            code="endpoint_not_allowed",
            side_effect_possible=False,
        )
    validated_team_id = ""
    if "team_id" in config:
        try:
            validated_team_id = validate_team_id(config["team_id"])
        except ValueError:
            raise VercelApiError(
                "Vercel team configuration is invalid",
                code="invalid_configuration",
                side_effect_possible=False,
            ) from None
    return VercelClient(base_url=base_url, token=token, team_id=validated_team_id)


async def _require_project(client: VercelClient, project_id: str) -> dict[str, Any]:
    project = await client.get_project(project_id)
    returned_id = _required_string(project.get("id"), field="project id", maximum=200)
    if returned_id != project_id:
        raise VercelApiError(
            "Vercel project does not match the requested project",
            code="project_scope_mismatch",
            side_effect_possible=False,
        )
    return project


async def _require_deployment(
    client: VercelClient,
    *,
    project_id: str,
    deployment_id: str,
) -> dict[str, Any]:
    deployment = await client.get_deployment(deployment_id)
    if _deployment_id(deployment) != deployment_id:
        raise VercelApiError(
            "Vercel deployment does not match the requested deployment",
            code="deployment_scope_mismatch",
            side_effect_possible=False,
        )
    if deployment_project_id(deployment) != project_id:
        raise VercelApiError(
            "Vercel deployment does not belong to the requested project",
            code="project_scope_mismatch",
            side_effect_possible=False,
        )
    return deployment


async def _project_list(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(ProjectListInput, payload)
    client = await _api(ctx, data.connection_id)
    projects, truncated = await client.list_projects(limit=data.limit)
    return ProjectListOutput(
        projects=[_project_info(project) for project in projects],
        truncated=truncated,
    )


async def _project_read(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(ProjectReadInput, payload)
    client = await _api(ctx, data.connection_id)
    project = await _require_project(client, data.project_id)
    return _project_info(project)


async def _deployment_list(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(DeploymentListInput, payload)
    client = await _api(ctx, data.connection_id)
    await _require_project(client, data.project_id)
    deployments, truncated = await client.list_deployments(
        project_id=data.project_id,
        limit=data.limit,
    )
    projected: list[DeploymentListItem] = []
    for deployment in deployments:
        item = _deployment_list_item(deployment)
        candidate = DeploymentListOutput(deployments=[*projected, item], truncated=True)
        if not _gateway_document_retained(
            candidate,
            maximum=MAX_DEPLOYMENT_LIST_OUTPUT_BYTES,
        ):
            truncated = True
            break
        projected.append(item)
    output = DeploymentListOutput(deployments=projected, truncated=truncated)
    if not _gateway_document_retained(
        output,
        maximum=MAX_DEPLOYMENT_LIST_OUTPUT_BYTES,
    ):
        raise _provider_shape("Vercel deployment-list output exceeded its safety limit")
    return output


async def _deployment_read(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(DeploymentReadInput, payload)
    client = await _api(ctx, data.connection_id)
    await _require_project(client, data.project_id)
    deployment = await _require_deployment(
        client,
        project_id=data.project_id,
        deployment_id=data.deployment_id,
    )
    return _deployment_info(deployment)


def _log_event(raw: dict[str, Any]) -> DeploymentLogEvent:
    timestamp = _timestamp(raw.get("created", raw.get("timestamp")), field="build-log timestamp")
    if timestamp is None:
        raise _provider_shape("Vercel returned a build-log event without a timestamp")
    message = raw.get("text", raw.get("message"))
    return DeploymentLogEvent(
        event_id=_display_string(raw.get("id"), field="build-log event id", maximum=200),
        timestamp=timestamp,
        event_type=_display_string(raw.get("type"), field="build-log type", maximum=100),
        level=_display_string(raw.get("level"), field="build-log level", maximum=50),
        message=_display_string(
            message,
            field="build-log message",
            maximum=_MAX_LOG_MESSAGE_CHARS,
        ),
    )


async def _deployment_logs(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(DeploymentLogsInput, payload)
    client = await _api(ctx, data.connection_id)
    await _require_project(client, data.project_id)
    await _require_deployment(
        client,
        project_id=data.project_id,
        deployment_id=data.deployment_id,
    )
    now_ms = int(time.time() * 1_000)
    if data.since is None and data.until is None:
        until = now_ms
        since = max(0, until - _LOG_WINDOW_MS)
    elif data.since is None:
        until = cast(int, data.until)
        since = max(0, until - _LOG_WINDOW_MS)
    elif data.until is None:
        since = data.since
        until = min(2**63 - 1, since + _LOG_WINDOW_MS)
    else:
        since = data.since
        until = data.until
    raw_events = await client.get_deployment_events(
        data.deployment_id,
        since=since,
        until=until,
        limit=data.limit,
    )
    events: list[DeploymentLogEvent] = []
    truncated = len(raw_events) >= data.limit
    for raw in raw_events[: data.limit]:
        event = _log_event(raw)
        if not since <= event.timestamp <= until:
            truncated = True
            continue
        candidate = DeploymentLogsOutput(events=[*events, event], truncated=True)
        if not _gateway_document_retained(candidate, maximum=MAX_LOG_OUTPUT_BYTES):
            truncated = True
            break
        events.append(event)
    output = DeploymentLogsOutput(events=events, truncated=truncated)
    # Keep a hard postcondition even if model serialization changes.
    if not _gateway_document_retained(output, maximum=MAX_LOG_OUTPUT_BYTES):
        raise _provider_shape("Vercel build-log output exceeded its safety limit")
    return output


def _environment_record(raw: dict[str, Any]) -> EnvironmentVariableMetadata:
    raw_targets = raw.get("target", [])
    if isinstance(raw_targets, str):
        raw_targets = [raw_targets]
    if not isinstance(raw_targets, list) or any(
        not isinstance(target, str) for target in raw_targets
    ):
        raise _provider_shape("Vercel returned invalid environment targets")
    key = raw.get("key")
    if not isinstance(key, str) or not key:
        raise _provider_shape("Vercel returned invalid environment metadata")
    return EnvironmentVariableMetadata(
        environment_id=_display_string(raw.get("id"), field="environment id", maximum=200),
        key=key[:256],
        name=_display_string(raw.get("name"), field="environment name", maximum=256),
        targets=[target[:50] for target in raw_targets[:10] if target],
        variable_type=_display_string(raw.get("type"), field="environment type", maximum=50),
        created_at=_timestamp(raw.get("createdAt"), field="environment creation timestamp"),
        updated_at=_timestamp(raw.get("updatedAt"), field="environment update timestamp"),
        git_branch=_display_string(
            raw.get("gitBranch"), field="environment Git branch", maximum=250
        ),
    )


async def _environment_metadata(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(EnvironmentMetadataInput, payload)
    client = await _api(ctx, data.connection_id)
    await _require_project(client, data.project_id)
    records = await client.get_environment(data.project_id)
    return EnvironmentMetadataOutput(
        variables=[_environment_record(record) for record in records[:_MAX_ENV_ROWS]],
        truncated=len(records) > _MAX_ENV_ROWS,
    )


def _git_source(
    project: dict[str, Any],
    *,
    requested_provider: str,
    requested_repository_id: str,
    ref: str,
) -> dict[str, Any]:
    link = project.get("link")
    if not isinstance(link, dict):
        raise VercelApiError(
            "Vercel project is not linked to the requested repository",
            code="repository_scope_mismatch",
            side_effect_possible=False,
        )
    link_type = link.get("type")
    if link_type in {"github", "github-limited"} and requested_provider == "github":
        repo_id = link.get("repoId")
        if (
            isinstance(repo_id, bool)
            or not isinstance(repo_id, int)
            or not 1 <= repo_id <= 2**63 - 1
            or str(repo_id) != requested_repository_id
        ):
            raise VercelApiError(
                "Vercel project is not linked to the requested repository",
                code="repository_scope_mismatch",
                side_effect_possible=False,
            )
        return {"type": "github", "ref": ref, "repoId": repo_id}
    if link_type == "gitlab" and requested_provider == "gitlab":
        project_id = link.get("projectId")
        if (
            not isinstance(project_id, str)
            or not project_id
            or len(project_id) > 200
            or project_id != requested_repository_id
        ):
            raise VercelApiError(
                "Vercel project is not linked to the requested repository",
                code="repository_scope_mismatch",
                side_effect_possible=False,
            )
        return {"type": "gitlab", "ref": ref, "projectId": project_id}
    if link_type == "bitbucket" and requested_provider == "bitbucket":
        repo_uuid = link.get("uuid")
        workspace_uuid = link.get("workspaceUuid")
        if (
            not isinstance(repo_uuid, str)
            or not repo_uuid
            or len(repo_uuid) > 200
            or repo_uuid != requested_repository_id
            or (
                workspace_uuid is not None
                and (
                    not isinstance(workspace_uuid, str)
                    or not workspace_uuid
                    or len(workspace_uuid) > 200
                )
            )
        ):
            raise VercelApiError(
                "Vercel project is not linked to the requested repository",
                code="repository_scope_mismatch",
                side_effect_possible=False,
            )
        source: dict[str, Any] = {
            "type": "bitbucket",
            "ref": ref,
            "repoUuid": repo_uuid,
        }
        if workspace_uuid is not None:
            source["workspaceUuid"] = workspace_uuid
        return source
    raise VercelApiError(
        "Vercel project is not linked to the requested repository",
        code="repository_scope_mismatch",
        side_effect_possible=False,
    )


def _mutation_output(
    deployment: dict[str, Any],
    *,
    action: str,
    project_id: str,
    target_override: str | None = None,
) -> DeploymentMutationOutput:
    parsed = _deployment_info(deployment)
    if parsed.project_id != project_id:
        raise VercelApiError(
            "Vercel mutation returned a deployment from another project",
            code="project_scope_mismatch",
        )
    return DeploymentMutationOutput(
        **parsed.model_dump(exclude={"inspector_url", "target"}),
        target=target_override if target_override is not None else parsed.target,
        action=action,
    )


async def _preview_create(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(PreviewCreateInput, payload)
    client = await _api(ctx, data.connection_id)
    project = await _require_project(client, data.project_id)
    source = _git_source(
        project,
        requested_provider=data.git_provider,
        requested_repository_id=data.repository_id,
        ref=data.ref,
    )
    created = await client.create_deployment(
        {
            "name": _required_string(project.get("name"), field="project name", maximum=256),
            "project": data.project_id,
            "target": "preview",
            "gitSource": source,
        }
    )
    return _mutation_output(created, action="preview_create", project_id=data.project_id)


async def _redeploy(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(RedeployInput, payload)
    client = await _api(ctx, data.connection_id)
    project = await _require_project(client, data.project_id)
    source = await _require_deployment(
        client,
        project_id=data.project_id,
        deployment_id=data.deployment_id,
    )
    target = _display_string(source.get("target"), field="deployment target", maximum=50)
    if target != data.environment:
        raise VercelApiError(
            "Vercel deployment environment does not match the requested environment",
            code="environment_scope_mismatch",
            side_effect_possible=False,
        )
    created = await client.redeploy(
        {
            "deploymentId": data.deployment_id,
            "name": _required_string(project.get("name"), field="project name", maximum=256),
            "target": data.environment,
            "meta": {"action": "redeploy"},
        }
    )
    return _mutation_output(created, action="redeploy", project_id=data.project_id)


async def _promote(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(PromoteInput, payload)
    client = await _api(ctx, data.connection_id)
    await _require_project(client, data.project_id)
    source = await _require_deployment(
        client,
        project_id=data.project_id,
        deployment_id=data.deployment_id,
    )
    response = await client.promote(data.project_id, data.deployment_id)
    deployment = response if "projectId" in response or "project" in response else source
    return _mutation_output(
        deployment,
        action="promote",
        project_id=data.project_id,
        target_override="production",
    )


async def _alias_assign(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(AliasAssignInput, payload)
    client = await _api(ctx, data.connection_id)
    await _require_project(client, data.project_id)
    deployment = await _require_deployment(
        client,
        project_id=data.project_id,
        deployment_id=data.deployment_id,
    )
    target = _display_string(deployment.get("target"), field="deployment target", maximum=50)
    if target != data.environment:
        raise VercelApiError(
            "Vercel deployment environment does not match the requested environment",
            code="environment_scope_mismatch",
            side_effect_possible=False,
        )
    await client.assign_alias(data.deployment_id, data.alias)
    return AliasAssignOutput(
        deployment_id=data.deployment_id,
        project_id=data.project_id,
        alias=data.alias,
        assigned=True,
    )


def _tool(
    *,
    name: str,
    description: str,
    risk: RiskLevel,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    executor: ToolExecutor,
    scope_keys: tuple[str, ...],
    required_grant_scope_keys: tuple[str, ...] | None = None,
    supports_approval: bool = False,
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
            scope_keys=scope_keys,
            required_grant_scope_keys=(
                required_grant_scope_keys if required_grant_scope_keys is not None else scope_keys
            ),
        ),
        executor,
    )


VERCEL_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor], ...] = (
    _tool(
        name="vercel.project.list",
        description="List display-safe Vercel project metadata.",
        risk=RiskLevel.READ,
        input_model=ProjectListInput,
        output_model=ProjectListOutput,
        executor=_project_list,
        scope_keys=("connection_id",),
    ),
    _tool(
        name="vercel.project.read",
        description="Read display-safe metadata for one Vercel project.",
        risk=RiskLevel.READ,
        input_model=ProjectReadInput,
        output_model=ProjectReadOutput,
        executor=_project_read,
        scope_keys=("connection_id", "project_id"),
    ),
    _tool(
        name="vercel.deployment.list",
        description="List bounded deployments for one Vercel project.",
        risk=RiskLevel.READ,
        input_model=DeploymentListInput,
        output_model=DeploymentListOutput,
        executor=_deployment_list,
        scope_keys=("connection_id", "project_id"),
    ),
    _tool(
        name="vercel.deployment.read",
        description="Read one deployment after verifying project ownership.",
        risk=RiskLevel.READ,
        input_model=DeploymentReadInput,
        output_model=DeploymentReadOutput,
        executor=_deployment_read,
        scope_keys=("connection_id", "project_id", "deployment_id"),
    ),
    _tool(
        name="vercel.deployment.logs.read",
        description="Read bounded Vercel deployment build logs for up to 24 hours.",
        risk=RiskLevel.READ,
        input_model=DeploymentLogsInput,
        output_model=DeploymentLogsOutput,
        executor=_deployment_logs,
        scope_keys=("connection_id", "project_id", "deployment_id"),
    ),
    _tool(
        name="vercel.environment_metadata.read",
        description="Read environment-variable names and metadata, never values.",
        risk=RiskLevel.READ,
        input_model=EnvironmentMetadataInput,
        output_model=EnvironmentMetadataOutput,
        executor=_environment_metadata,
        scope_keys=("connection_id", "project_id"),
    ),
    _tool(
        name="vercel.deployment.preview.create",
        description="Create a preview deployment from the project's verified linked repository.",
        risk=RiskLevel.ELEVATED,
        input_model=PreviewCreateInput,
        output_model=DeploymentMutationOutput,
        executor=_preview_create,
        scope_keys=("connection_id", "project_id", "environment", "repository_id", "ref"),
        required_grant_scope_keys=(
            "connection_id",
            "project_id",
            "environment",
            "repository_id",
        ),
        supports_approval=True,
    ),
    _tool(
        name="vercel.deployment.redeploy",
        description="Redeploy an existing deployment in its verified environment.",
        risk=RiskLevel.DESTRUCTIVE,
        input_model=RedeployInput,
        output_model=DeploymentMutationOutput,
        executor=_redeploy,
        scope_keys=("connection_id", "project_id", "deployment_id", "environment"),
        supports_approval=True,
    ),
    _tool(
        name="vercel.deployment.promote",
        description="Promote a verified deployment to production.",
        risk=RiskLevel.DESTRUCTIVE,
        input_model=PromoteInput,
        output_model=DeploymentMutationOutput,
        executor=_promote,
        scope_keys=("connection_id", "project_id", "deployment_id", "environment"),
        supports_approval=True,
    ),
    _tool(
        name="vercel.deployment.alias.assign",
        description="Assign a production alias to a verified deployment.",
        risk=RiskLevel.DESTRUCTIVE,
        input_model=AliasAssignInput,
        output_model=AliasAssignOutput,
        executor=_alias_assign,
        scope_keys=(
            "connection_id",
            "project_id",
            "deployment_id",
            "environment",
            "alias",
        ),
        supports_approval=True,
    ),
)
