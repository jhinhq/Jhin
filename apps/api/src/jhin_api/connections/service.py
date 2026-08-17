"""Connection lifecycle business logic (plan 6.9, 11).

Credential flow (plan 13.4/48.1): credential fields arrive once in the
create/rotate request, are serialized to JSON and stored through
``SecretStore`` (AES-256-GCM envelope), and the connection row only keeps the
secret id. Decryption happens transiently inside ``verify`` here and inside
tool executors at execution time — plaintext is never returned by any route.
"""

from __future__ import annotations

import json
import secrets as stdlib_secrets
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_connectors import ConnectionHealth, Connector, VerifyContext, default_registry
from jhin_db.models import Connection, ToolCall
from jhin_db.models.connection import new_public_id
from jhin_domain import ConnectionStatus, SecretType
from jhin_secrets import SecretCrypto, SecretStore

_REGISTRY = default_registry()


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def get_connector(connector_type: str) -> Connector:
    connector = _REGISTRY.get(connector_type)
    if connector is None:
        raise _bad_request(f"Unknown connector type '{connector_type}'")
    return connector


def _validate_credentials(
    connector: Connector, auth_type: str, credentials: dict[str, str]
) -> None:
    """Check the submitted fields against the manifest's auth scheme: all
    required fields present and non-empty, no undeclared fields."""
    scheme = connector.manifest.auth_scheme(auth_type)
    if scheme is None:
        allowed = ", ".join(s.type for s in connector.manifest.auth_schemes)
        raise _bad_request(f"Auth type '{auth_type}' is not supported (expected one of: {allowed})")
    declared = {field.name for field in scheme.secret_fields}
    unknown = sorted(set(credentials) - declared)
    if unknown:
        raise _bad_request(f"Unknown credential fields: {', '.join(unknown)}")
    missing = [name for name in scheme.required_field_names() if not credentials.get(name)]
    if missing:
        raise _bad_request(f"Missing required credential fields: {', '.join(missing)}")


def _validate_config(connector: Connector, config: dict[str, object]) -> None:
    declared = {field.name for field in connector.manifest.config_fields}
    unknown = sorted(set(config) - declared)
    if unknown:
        raise _bad_request(f"Unknown config fields: {', '.join(unknown)}")
    missing = [
        field.name
        for field in connector.manifest.config_fields
        if field.required and not config.get(field.name)
    ]
    if missing:
        raise _bad_request(f"Missing required config fields: {', '.join(missing)}")


async def list_connections(db: AsyncSession, workspace_id: UUID) -> list[Connection]:
    rows = await db.scalars(
        select(Connection)
        .where(Connection.workspace_id == workspace_id)
        .order_by(Connection.created_at)
    )
    return list(rows)


async def get_connection(db: AsyncSession, workspace_id: UUID, connection_id: UUID) -> Connection:
    connection = await db.scalar(
        select(Connection).where(
            Connection.id == connection_id, Connection.workspace_id == workspace_id
        )
    )
    if connection is None:
        raise _not_found()
    return connection


async def create_connection(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    *,
    connector_type: str,
    name: str,
    auth_type: str,
    credentials: dict[str, str],
    config: dict[str, object],
    request_id: UUID,
    ip_hash: str,
) -> tuple[Connection, str | None]:
    """Create a connection; returns it plus the webhook signing secret
    plaintext (shown exactly once) when the connector supports webhooks."""
    connector = get_connector(connector_type)
    _validate_credentials(connector, auth_type, credentials)
    _validate_config(connector, config)

    public_id = new_public_id()
    store = SecretStore(db, crypto)
    credential_secret = await store.create(
        workspace_id=ctx.workspace_id,
        name=f"connection/{public_id}/credentials",
        plaintext=json.dumps(credentials),
        secret_type=SecretType.CONNECTION_CREDENTIALS,
        created_by_user_id=ctx.user.id,
    )

    webhook_plaintext: str | None = None
    webhook_secret_id: UUID | None = None
    if connector.manifest.supports_webhooks:
        webhook_plaintext = stdlib_secrets.token_urlsafe(32)
        webhook_secret = await store.create(
            workspace_id=ctx.workspace_id,
            name=f"connection/{public_id}/webhook",
            plaintext=webhook_plaintext,
            secret_type=SecretType.WEBHOOK_SECRET,
            created_by_user_id=ctx.user.id,
        )
        webhook_secret_id = webhook_secret.id

    connection = Connection(
        workspace_id=ctx.workspace_id,
        connector_type=connector_type,
        name=name,
        auth_type=auth_type,
        public_id=public_id,
        encrypted_secret_id=credential_secret.id,
        webhook_secret_id=webhook_secret_id,
        config_json=dict(config),
        created_by_user_id=ctx.user.id,
    )
    db.add(connection)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A connection with this name already exists in the workspace",
        ) from exc
    audit.record(
        db,
        action="connection.created",
        target_type="connection",
        target_id=connection.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"connector_type": connector_type, "name": name, "auth_type": auth_type},
    )
    await db.commit()
    return connection, webhook_plaintext


async def verify_connection(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    connection_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> tuple[Connection, ConnectionHealth]:
    """Run the connector's live health check and persist the outcome
    (status, last_verified_at, last_error — plan 6.9)."""
    connection = await get_connection(db, ctx.workspace_id, connection_id)
    connector = get_connector(connection.connector_type)
    if connection.encrypted_secret_id is None:
        raise _bad_request("Connection has no stored credential")

    store = SecretStore(db, crypto)
    plaintext = await store.reveal(ctx.workspace_id, connection.encrypted_secret_id)
    credentials = json.loads(plaintext)
    health = await connector.verify_connection(
        VerifyContext(
            auth_type=connection.auth_type,
            credentials=credentials,
            config=dict(connection.config_json),
        )
    )
    connection.last_verified_at = datetime.now(UTC)
    if health.ok:
        connection.status = ConnectionStatus.ACTIVE.value
        connection.last_error = None
    else:
        connection.status = ConnectionStatus.ERROR.value
        connection.last_error = health.message[:2000]
    audit.record(
        db,
        action="connection.verified",
        target_type="connection",
        target_id=connection.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"ok": health.ok, "status": connection.status},
    )
    await db.commit()
    return connection, health


async def rotate_credentials(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    connection_id: UUID,
    *,
    credentials: dict[str, str],
    request_id: UUID,
    ip_hash: str,
) -> Connection:
    connection = await get_connection(db, ctx.workspace_id, connection_id)
    connector = get_connector(connection.connector_type)
    _validate_credentials(connector, connection.auth_type, credentials)
    if connection.encrypted_secret_id is None:
        raise _bad_request("Connection has no stored credential to rotate")
    store = SecretStore(db, crypto)
    await store.rotate(ctx.workspace_id, connection.encrypted_secret_id, json.dumps(credentials))
    # The new credential is unproven: reset health so operators re-verify.
    connection.status = ConnectionStatus.ACTIVE.value
    connection.last_error = None
    connection.last_verified_at = None
    audit.record(
        db,
        action="connection.credentials_rotated",
        target_type="connection",
        target_id=connection.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": connection.name},
    )
    await db.commit()
    return connection


async def set_status(
    db: AsyncSession,
    ctx: WorkspaceContext,
    connection_id: UUID,
    *,
    disabled: bool,
    request_id: UUID,
    ip_hash: str,
) -> Connection:
    connection = await get_connection(db, ctx.workspace_id, connection_id)
    connection.status = (
        ConnectionStatus.DISABLED.value if disabled else ConnectionStatus.ACTIVE.value
    )
    audit.record(
        db,
        action="connection.disabled" if disabled else "connection.enabled",
        target_type="connection",
        target_id=connection.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": connection.name},
    )
    await db.commit()
    return connection


async def delete_connection(
    db: AsyncSession,
    ctx: WorkspaceContext,
    connection_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> None:
    connection = await get_connection(db, ctx.workspace_id, connection_id)
    store_ids = [connection.encrypted_secret_id, connection.webhook_secret_id]
    audit.record(
        db,
        action="connection.deleted",
        target_type="connection",
        target_id=connection.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"connector_type": connection.connector_type, "name": connection.name},
    )
    await db.delete(connection)
    # The connection's secrets have no other consumers; remove them too so no
    # orphaned ciphertext lingers.
    from jhin_db.models import Secret

    for secret_id in store_ids:
        if secret_id is not None:
            secret = await db.get(Secret, secret_id)
            if secret is not None:
                await db.delete(secret)
    await db.commit()


async def recent_tool_calls(
    db: AsyncSession, workspace_id: UUID, connection_id: UUID, *, limit: int = 20
) -> list[ToolCall]:
    """Latest tool calls that used this connection (plan 17.9 detail view)."""
    rows = await db.scalars(
        select(ToolCall)
        .where(ToolCall.workspace_id == workspace_id, ToolCall.connection_id == connection_id)
        .order_by(ToolCall.created_at.desc())
        .limit(min(limit, 100))
    )
    return list(rows)
