"""Routes for the connector catalog and workspace connections (plan 6.9, 17.9).

Connection management is admin-only (plan 20.2). The connector catalog is
static manifest data, readable by any authenticated user for the gallery.
No route here ever returns credential plaintext; the webhook signing secret
appears once, in the creation response.
"""

from collections.abc import Sequence
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.catalog.service import builtin_logo_url
from jhin_api.connections import service
from jhin_api.connections.schemas import (
    CatalogAppOut,
    ConnectionAccessSummaryOut,
    ConnectionAuthorizedByOut,
    ConnectionConfigUpdate,
    ConnectionCreate,
    ConnectionCreated,
    ConnectionOut,
    ConnectionToolsOut,
    ConnectorOut,
    CredentialsRotate,
    ToolRiskOverridesWrite,
    VerifyOut,
    WebhookSecretWrite,
    WebhookSetupOut,
)
from jhin_api.deps import (
    AdminCtx,
    CurrentAuth,
    DbSession,
    OAuthHttpClientDep,
    SecretCryptoDep,
    get_settings_dep,
)
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.oauth import service as oauth_service
from jhin_api.oauth.schemas import OAuthStartIn, OAuthStartOut
from jhin_api.security.csrf import csrf_protect
from jhin_api.settings import Settings
from jhin_api.tasks.schemas import ToolCallOut
from jhin_connectors import default_registry
from jhin_connectors.catalog import load_catalog
from jhin_db.models import Connection, User
from jhin_domain import ConnectionStatus
from jhin_oauth.lifecycle import ConnectionTokenService
from jhin_tools.sanitize import strict_json_loads

MAX_SENSITIVE_CONNECTION_BODY_BYTES = 65_536

OAuthSettingsDep = Annotated[Settings, Depends(get_settings_dep)]

catalog_router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/connections",
    tags=["connections"],
    dependencies=[Depends(csrf_protect)],
)


def json_request_body(model: type[BaseModel]) -> dict[str, Any]:
    """Keep request schemas visible after moving validation behind a safe boundary.

    Public because the OAuth router reads its credential-bearing bodies
    through the same bounded path: one implementation, one set of limits, one
    place to audit."""
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": model.model_json_schema(),
                }
            },
        }
    }


def optional_nonnegative_content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


async def read_sensitive_body(request: Request, *, too_large_detail: str) -> bytes:
    content_length = optional_nonnegative_content_length(request)
    if content_length is not None and content_length > MAX_SENSITIVE_CONNECTION_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=too_large_detail,
        )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_SENSITIVE_CONNECTION_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=too_large_detail,
            )
        body.extend(chunk)
    return bytes(body)


async def parse_sensitive_json[SensitivePayloadT: BaseModel](
    request: Request,
    model: type[SensitivePayloadT],
    *,
    invalid_detail: str,
    too_large_detail: str,
) -> SensitivePayloadT:
    body = await read_sensitive_body(request, too_large_detail=too_large_detail)
    try:
        decoded = strict_json_loads(body.decode("utf-8"))
        return model.model_validate(decoded, strict=True)
    except (ValueError, RecursionError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=invalid_detail,
        ) from None


@catalog_router.get("")
async def list_connectors(_auth: CurrentAuth) -> list[ConnectorOut]:
    """Manifests of every installed connector (gallery data, plan 17.9)."""
    return [
        ConnectorOut.model_validate(connector.manifest.model_dump())
        for connector in default_registry()
    ]


@catalog_router.get("/catalog")
async def list_catalog(_auth: CurrentAuth, settings: OAuthSettingsDep) -> list[CatalogAppOut]:
    """The curated Apps library: known apps with native connectors or MCP
    endpoints (docs/architecture/mcp.md). Static public data.

    Entries flagged ``dev_only`` (the dev stack's test doubles) never leave a
    production-like install. ``logo_url`` is the same-origin icon-proxy path,
    never the upstream URL: the entry's ``icon_url`` is an instruction to the
    proxy and stays on the server (``CatalogAppOut`` declares no such field,
    so the dump's copy is dropped on validation)."""
    return [
        CatalogAppOut.model_validate({**entry.model_dump(), "logo_url": builtin_logo_url(entry)})
        for entry in load_catalog()
        if not (entry.dev_only and settings.is_production_like)
    ]


def _out(
    connection: Connection, *, authorized_by: ConnectionAuthorizedByOut | None = None
) -> ConnectionOut:
    output = ConnectionOut.model_validate(connection, from_attributes=True)
    return output.model_copy(
        update={
            "config_json": service.public_connection_config(connection),
            "webhook_secret_configured": connection.webhook_secret_id is not None,
            "authorized_by": authorized_by,
            "needs_reauth": connection.status == ConnectionStatus.NEEDS_REAUTH.value,
        }
    )


async def _authorizing_users(
    db: AsyncSession, connections: Sequence[Connection]
) -> dict[UUID, ConnectionAuthorizedByOut]:
    """Display names for whoever authorized these connections, in one query.

    Resolved in a batch rather than per row: the Apps page lists every
    connection a workspace has, and "whose access is this?" should not cost a
    query each to answer.
    """
    ids = {
        connection.oauth_authorized_by_user_id
        for connection in connections
        if connection.oauth_authorized_by_user_id is not None
    }
    if not ids:
        return {}
    rows = (await db.execute(select(User.id, User.display_name).where(User.id.in_(ids)))).all()
    return {
        user_id: ConnectionAuthorizedByOut(user_id=user_id, display_name=display_name)
        for user_id, display_name in rows
    }


async def serialize_connection(db: AsyncSession, connection: Connection) -> ConnectionOut:
    names = await _authorizing_users(db, [connection])
    return _out(
        connection,
        authorized_by=names.get(connection.oauth_authorized_by_user_id)
        if connection.oauth_authorized_by_user_id is not None
        else None,
    )


async def serialize_connections(
    db: AsyncSession, connections: Sequence[Connection]
) -> list[ConnectionOut]:
    names = await _authorizing_users(db, connections)
    return [
        _out(
            connection,
            authorized_by=names.get(connection.oauth_authorized_by_user_id)
            if connection.oauth_authorized_by_user_id is not None
            else None,
        )
        for connection in connections
    ]


def webhook_url_path(connection: Connection) -> str:
    return f"/api/v1/webhooks/{connection.connector_type}/{connection.public_id}"


def _webhook_setup(connection: Connection, secret: str | None) -> WebhookSetupOut | None:
    connector = service.get_connector(connection.connector_type)
    mode = service.webhook_secret_mode(connector)
    if mode == "none":
        return None
    return WebhookSetupOut(
        url_path=webhook_url_path(connection),
        secret=secret,
        secret_mode=mode,
        signature_algorithm=connector.manifest.webhook_signature_algorithm,
        help=connector.manifest.webhook_setup_help,
    )


@router.get("")
async def list_connections(ctx: AdminCtx, db: DbSession) -> list[ConnectionOut]:
    return await serialize_connections(db, await service.list_connections(db, ctx.workspace_id))


@router.post(
    "",
    status_code=201,
    openapi_extra=json_request_body(ConnectionCreate),
)
async def create_connection(
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
) -> ConnectionCreated:
    payload = await parse_sensitive_json(
        request,
        ConnectionCreate,
        invalid_detail="Connection payload is invalid",
        too_large_detail="Connection payload is too large",
    )
    connection, webhook_secret = await service.create_connection(
        db,
        crypto,
        ctx,
        connector_type=payload.connector_type,
        name=payload.name,
        auth_type=payload.auth_type,
        credentials=payload.credentials,
        config=payload.config,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    webhook = _webhook_setup(connection, webhook_secret)
    return ConnectionCreated(connection=await serialize_connection(db, connection), webhook=webhook)


@router.get("/{connection_id}")
async def get_connection(connection_id: UUID, ctx: AdminCtx, db: DbSession) -> ConnectionOut:
    return await serialize_connection(
        db, await service.get_connection(db, ctx.workspace_id, connection_id)
    )


@router.get("/{connection_id}/access-summary")
async def get_connection_access_summary(
    connection_id: UUID, ctx: AdminCtx, db: DbSession
) -> ConnectionAccessSummaryOut:
    return ConnectionAccessSummaryOut.model_validate(
        await service.connection_access_summary(db, ctx.workspace_id, connection_id)
    )


@router.get("/{connection_id}/tool-calls")
async def connection_tool_calls(
    connection_id: UUID, ctx: AdminCtx, db: DbSession
) -> list[ToolCallOut]:
    """Recent tool usage through this connection (plan 17.9 detail view)."""
    await service.get_connection(db, ctx.workspace_id, connection_id)
    rows = await service.recent_tool_calls(db, ctx.workspace_id, connection_id)
    return [ToolCallOut.model_validate(row, from_attributes=True) for row in rows]


@router.get("/{connection_id}/tools")
async def connection_tools(
    connection_id: UUID,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    refresh: bool = False,
) -> ConnectionToolsOut:
    """Tools reachable through this connection with their enforced risk.
    MCP connections list their discovered tools (discovering once when no
    discovery is stored yet, or again with ``?refresh=true``)."""
    return ConnectionToolsOut.model_validate(
        await service.list_connection_tools(
            db,
            crypto,
            ctx,
            connection_id,
            refresh=refresh,
            request_id=req_id(request),
            ip_hash=ip_hash(request),
        )
    )


@router.patch("/{connection_id}/tools")
async def update_tool_risk_overrides(
    connection_id: UUID,
    payload: ToolRiskOverridesWrite,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
) -> ConnectionToolsOut:
    """Raise or lower the risk level of individual discovered tools."""
    return ConnectionToolsOut.model_validate(
        await service.update_tool_risk_overrides(
            db,
            ctx,
            connection_id,
            overrides=dict(payload.tool_risk_overrides),
            request_id=req_id(request),
            ip_hash=ip_hash(request),
        )
    )


@router.get("/{connection_id}/metadata")
async def connection_metadata(
    connection_id: UUID, ctx: AdminCtx, db: DbSession, crypto: SecretCryptoDep
) -> dict[str, object]:
    """Display-safe provider metadata (e.g. Linear teams + workflow states)
    for UI pickers like the trigger builder (plan 17.10). Never credentials."""
    return await service.fetch_metadata(db, crypto, ctx, connection_id)


@router.post("/{connection_id}/verify")
async def verify_connection(
    connection_id: UUID,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
) -> VerifyOut:
    connection, health = await service.verify_connection(
        db, crypto, ctx, connection_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return VerifyOut(
        ok=health.ok, message=health.message, status=connection.status, details=health.details
    )


@router.post("/{connection_id}/reauthorize", status_code=201)
async def reauthorize_connection(
    connection_id: UUID,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    settings: OAuthSettingsDep,
    http_client: OAuthHttpClientDep,
) -> OAuthStartOut:
    """Start a fresh authorization for a connection that needs reconnecting.

    Keeps the connection row — its id is what every grant, trigger, and
    recorded tool call points at — and replaces only the grant behind it. This
    is what the amber "needs reconnecting" banner's one button calls, and it
    is why reconnecting an app does not silently revoke every agent's access
    to it.

    Only the *manifest-declared* settings are carried forward. ``config_json``
    also holds server-side bookkeeping — the recorded issuer and token
    endpoint, the discovered tool list, any pending step-up scope — and none of
    it belongs in a draft: the pending-authorization store refuses a draft key
    that looks like a credential (``oauth_token_endpoint`` contains "token"),
    and ``normalize_config`` refuses any key the manifest does not declare. The
    values that actually matter on the way back in are re-derived from the
    connection row itself, not from the draft.
    """
    connection = await service.get_connection(db, ctx.workspace_id, connection_id)
    return await oauth_service.start_authorization(
        db,
        crypto,
        ctx,
        http_client,
        settings,
        OAuthStartIn(
            connector_type=connection.connector_type,
            name=connection.name,
            config=service.public_connection_config(connection),
            connection_id=connection.id,
            provider_key=oauth_service.provider_key_for(connection),
        ),
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )


@router.post(
    "/{connection_id}/rotate",
    openapi_extra=json_request_body(CredentialsRotate),
)
async def rotate_credentials(
    connection_id: UUID,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
) -> ConnectionOut:
    payload = await parse_sensitive_json(
        request,
        CredentialsRotate,
        invalid_detail="Credential rotation payload is invalid",
        too_large_detail="Credential rotation payload is too large",
    )
    connection = await service.rotate_credentials(
        db,
        crypto,
        ctx,
        connection_id,
        credentials=payload.credentials,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return await serialize_connection(db, connection)


@router.put(
    "/{connection_id}/webhook-secret",
    status_code=204,
    openapi_extra=json_request_body(WebhookSecretWrite),
)
async def store_webhook_secret(
    connection_id: UUID,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
) -> None:
    payload = await parse_sensitive_json(
        request,
        WebhookSecretWrite,
        invalid_detail="Webhook secret payload is invalid",
        too_large_detail="Webhook secret payload is too large",
    )
    await service.set_webhook_secret(
        db,
        crypto,
        ctx,
        connection_id,
        secret=payload.secret,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )


@router.patch("/{connection_id}/config")
async def update_connection_config(
    connection_id: UUID,
    payload: ConnectionConfigUpdate,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
) -> ConnectionOut:
    """Replace the connection's public settings — for a CLI Sandbox, the
    repositories it may use and the GitHub connection it borrows from."""
    connection = await service.update_config(
        db,
        ctx,
        connection_id,
        config=payload.config,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return await serialize_connection(db, connection)


@router.post("/{connection_id}/disable")
async def disable_connection(
    connection_id: UUID, request: Request, ctx: AdminCtx, db: DbSession
) -> ConnectionOut:
    connection = await service.set_status(
        db, ctx, connection_id, disabled=True, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return await serialize_connection(db, connection)


@router.post("/{connection_id}/enable")
async def enable_connection(
    connection_id: UUID, request: Request, ctx: AdminCtx, db: DbSession
) -> ConnectionOut:
    connection = await service.set_status(
        db, ctx, connection_id, disabled=False, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return await serialize_connection(db, connection)


@router.delete("/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: UUID,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    http_client: OAuthHttpClientDep,
) -> None:
    """Disconnect an app, telling the provider so when it was an OAuth grant."""
    await service.delete_connection(
        db,
        ctx,
        connection_id,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
        tokens=ConnectionTokenService(db, crypto, http_client),
    )
