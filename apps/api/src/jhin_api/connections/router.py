"""Routes for the connector catalog and workspace connections (plan 6.9, 17.9).

Connection management is admin-only (plan 20.2). The connector catalog is
static manifest data, readable by any authenticated user for the gallery.
No route here ever returns credential plaintext; the webhook signing secret
appears once, in the creation response.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from jhin_api.connections import service
from jhin_api.connections.schemas import (
    ConnectionCreate,
    ConnectionCreated,
    ConnectionOut,
    ConnectorOut,
    CredentialsRotate,
    VerifyOut,
    WebhookSetupOut,
)
from jhin_api.deps import AdminCtx, CurrentAuth, DbSession, SecretCryptoDep
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.security.csrf import csrf_protect
from jhin_api.tasks.schemas import ToolCallOut
from jhin_connectors import default_registry
from jhin_db.models import Connection

catalog_router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/connections",
    tags=["connections"],
    dependencies=[Depends(csrf_protect)],
)


@catalog_router.get("")
async def list_connectors(_auth: CurrentAuth) -> list[ConnectorOut]:
    """Manifests of every installed connector (gallery data, plan 17.9)."""
    return [
        ConnectorOut.model_validate(connector.manifest.model_dump())
        for connector in default_registry()
    ]


def _out(connection: Connection) -> ConnectionOut:
    return ConnectionOut.model_validate(connection, from_attributes=True)


def webhook_url_path(connection: Connection) -> str:
    return f"/api/v1/webhooks/{connection.connector_type}/{connection.public_id}"


@router.get("")
async def list_connections(ctx: AdminCtx, db: DbSession) -> list[ConnectionOut]:
    return [_out(row) for row in await service.list_connections(db, ctx.workspace_id)]


@router.post("", status_code=201)
async def create_connection(
    payload: ConnectionCreate,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
) -> ConnectionCreated:
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
    webhook = (
        WebhookSetupOut(url_path=webhook_url_path(connection), secret=webhook_secret)
        if webhook_secret is not None
        else None
    )
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


@router.post("/{connection_id}/rotate")
async def rotate_credentials(
    connection_id: UUID,
    payload: CredentialsRotate,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
) -> ConnectionOut:
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
