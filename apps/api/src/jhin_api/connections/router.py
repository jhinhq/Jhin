"""Routes for the connector catalog and workspace connections (plan 6.9, 17.9).

Connection management is admin-only (plan 20.2). The connector catalog is
static manifest data, readable by any authenticated user for the gallery.
No route here ever returns credential plaintext; the webhook signing secret
appears once, in the creation response.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from jhin_api.connections import service
from jhin_api.connections.schemas import (
    ConnectionCreate,
    ConnectionCreated,
    ConnectionOut,
    ConnectorOut,
    CredentialsRotate,
    VerifyOut,
    WebhookSecretWrite,
    WebhookSetupOut,
)
from jhin_api.deps import AdminCtx, CurrentAuth, DbSession, SecretCryptoDep
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.security.csrf import csrf_protect
from jhin_api.tasks.schemas import ToolCallOut
from jhin_connectors import default_registry
from jhin_db.models import Connection
from jhin_tools.sanitize import strict_json_loads

MAX_SENSITIVE_CONNECTION_BODY_BYTES = 65_536

catalog_router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/connections",
    tags=["connections"],
    dependencies=[Depends(csrf_protect)],
)


def _json_request_body(model: type[BaseModel]) -> dict[str, Any]:
    """Keep request schemas visible after moving validation behind a safe boundary."""
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


def _optional_nonnegative_content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


async def _read_sensitive_body(request: Request, *, too_large_detail: str) -> bytes:
    content_length = _optional_nonnegative_content_length(request)
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


async def _parse_sensitive_json[SensitivePayloadT: BaseModel](
    request: Request,
    model: type[SensitivePayloadT],
    *,
    invalid_detail: str,
    too_large_detail: str,
) -> SensitivePayloadT:
    body = await _read_sensitive_body(request, too_large_detail=too_large_detail)
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


def _out(connection: Connection) -> ConnectionOut:
    output = ConnectionOut.model_validate(connection, from_attributes=True)
    return output.model_copy(
        update={
            "config_json": service.public_connection_config(connection),
            "webhook_secret_configured": connection.webhook_secret_id is not None,
        }
    )


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
    return [_out(row) for row in await service.list_connections(db, ctx.workspace_id)]


@router.post(
    "",
    status_code=201,
    openapi_extra=_json_request_body(ConnectionCreate),
)
async def create_connection(
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
) -> ConnectionCreated:
    payload = await _parse_sensitive_json(
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
    return ConnectionCreated(connection=_out(connection), webhook=webhook)


@router.get("/{connection_id}")
async def get_connection(connection_id: UUID, ctx: AdminCtx, db: DbSession) -> ConnectionOut:
    return _out(await service.get_connection(db, ctx.workspace_id, connection_id))


@router.get("/{connection_id}/tool-calls")
async def connection_tool_calls(
    connection_id: UUID, ctx: AdminCtx, db: DbSession
) -> list[ToolCallOut]:
    """Recent tool usage through this connection (plan 17.9 detail view)."""
    await service.get_connection(db, ctx.workspace_id, connection_id)
    rows = await service.recent_tool_calls(db, ctx.workspace_id, connection_id)
    return [ToolCallOut.model_validate(row, from_attributes=True) for row in rows]


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


@router.post(
    "/{connection_id}/rotate",
    openapi_extra=_json_request_body(CredentialsRotate),
)
async def rotate_credentials(
    connection_id: UUID,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
) -> ConnectionOut:
    payload = await _parse_sensitive_json(
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
    return _out(connection)


@router.put(
    "/{connection_id}/webhook-secret",
    status_code=204,
    openapi_extra=_json_request_body(WebhookSecretWrite),
)
async def store_webhook_secret(
    connection_id: UUID,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
) -> None:
    payload = await _parse_sensitive_json(
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


@router.post("/{connection_id}/disable")
async def disable_connection(
    connection_id: UUID, request: Request, ctx: AdminCtx, db: DbSession
) -> ConnectionOut:
    connection = await service.set_status(
        db, ctx, connection_id, disabled=True, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return _out(connection)


@router.post("/{connection_id}/enable")
async def enable_connection(
    connection_id: UUID, request: Request, ctx: AdminCtx, db: DbSession
) -> ConnectionOut:
    connection = await service.set_status(
        db, ctx, connection_id, disabled=False, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return _out(connection)


@router.delete("/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: UUID, request: Request, ctx: AdminCtx, db: DbSession
) -> None:
    await service.delete_connection(
        db, ctx, connection_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )
