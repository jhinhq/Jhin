"""Connection lifecycle business logic (plan 6.9, 11).

Credential flow (plan 13.4/48.1): credential fields arrive once in the
create/rotate request, are serialized to JSON and stored through
``SecretStore`` (AES-256-GCM envelope), and the connection row only keeps the
secret id. Decryption happens transiently inside ``verify`` here and inside
tool executors at execution time — plaintext is never returned by any route.
"""

from __future__ import annotations

import json
import math
import secrets as stdlib_secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_connectors import (
    ConnectionHealth,
    Connector,
    VerifyContext,
    WebhookSecretMode,
    default_registry,
    normalize_config,
)
from jhin_db.models import Connection, ToolCall
from jhin_db.models.connection import new_public_id
from jhin_domain import ConnectionStatus, SecretType
from jhin_secrets import (
    SecretCrypto,
    SecretMaterialError,
    SecretStore,
    decode_string_secret_map,
    get_redactor,
)

_REGISTRY = default_registry()
_MAX_PROVIDER_OUTPUT_STRING_CHARS = 2_000
_MAX_PROVIDER_OUTPUT_BYTES = 32_768
_MAX_PROVIDER_OUTPUT_ITEMS = 256
_MAX_PROVIDER_OUTPUT_DEPTH = 16


class _ProviderOutputLimitError(ValueError):
    pass


@dataclass
class _ProviderOutputBudget:
    items: int = 0

    def consume_item(self) -> None:
        self.items += 1
        if self.items > _MAX_PROVIDER_OUTPUT_ITEMS:
            raise _ProviderOutputLimitError("provider output exceeds the item limit")


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _decode_stored_credentials(plaintext: str) -> dict[str, str]:
    try:
        return decode_string_secret_map(plaintext)
    except SecretMaterialError:
        raise _bad_request("Stored connection credential is malformed") from None


def _safe_provider_text(value: str) -> str:
    """Redact the complete provider value before applying the persistence cap."""
    return get_redactor().redact_text(value)[:_MAX_PROVIDER_OUTPUT_STRING_CHARS]


def _safe_provider_key(value: object) -> str:
    try:
        rendered = str(value)
    except Exception:
        rendered = "[unsupported provider key]"
    return _safe_provider_text(rendered)


def _unique_provider_key(key: str, existing: dict[str, object]) -> str:
    """Preserve values when stringification/redaction makes keys collide."""
    if key not in existing:
        return key
    collision = 2
    while True:
        suffix = f"#{collision}"
        candidate = f"{key[: _MAX_PROVIDER_OUTPUT_STRING_CHARS - len(suffix)]}{suffix}"
        if candidate not in existing:
            return candidate
        collision += 1


def _sanitize_provider_value(
    value: object,
    *,
    budget: _ProviderOutputBudget,
    depth: int = 0,
) -> object:
    """Recursively scrub provider-controlled metadata without changing containers."""
    if depth > _MAX_PROVIDER_OUTPUT_DEPTH:
        raise _ProviderOutputLimitError("provider output exceeds the depth limit")
    if isinstance(value, str):
        return _safe_provider_text(value)
    if isinstance(value, dict):
        sanitized: dict[str, object] = {}
        for key, item in value.items():
            budget.consume_item()
            safe_key = _unique_provider_key(_safe_provider_key(key), sanitized)
            sanitized[safe_key] = _sanitize_provider_value(
                item,
                budget=budget,
                depth=depth + 1,
            )
        return sanitized
    if isinstance(value, list):
        sanitized_list: list[object] = []
        for item in value:
            budget.consume_item()
            sanitized_list.append(_sanitize_provider_value(item, budget=budget, depth=depth + 1))
        return sanitized_list
    if isinstance(value, tuple):
        sanitized_items: list[object] = []
        for item in value:
            budget.consume_item()
            sanitized_items.append(_sanitize_provider_value(item, budget=budget, depth=depth + 1))
        return tuple(sanitized_items)
    if isinstance(value, (int, bool)) or value is None:
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return _safe_provider_key(value)


def _sanitize_provider_document(value: object) -> object:
    """Sanitize one provider document and enforce aggregate in-process bounds."""
    try:
        sanitized = _sanitize_provider_value(
            value,
            budget=_ProviderOutputBudget(),
        )
        encoded = json.dumps(
            sanitized,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except _ProviderOutputLimitError:
        raise
    except Exception:
        raise _ProviderOutputLimitError("provider output is not safely serializable") from None
    if len(encoded) > _MAX_PROVIDER_OUTPUT_BYTES:
        raise _ProviderOutputLimitError("provider output exceeds the byte limit")
    return sanitized


def _unsafe_provider_output() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Provider returned an unsafe or oversized response",
    )


def get_connector(connector_type: str) -> Connector:
    connector = _REGISTRY.get(connector_type)
    if connector is None:
        raise _bad_request(f"Unknown connector type '{connector_type}'")
    return connector


def webhook_secret_mode(connector: Connector) -> WebhookSecretMode:
    return connector.manifest.webhook_secret_mode


def public_connection_config(connection: Connection) -> dict[str, object]:
    """Return only manifest-declared settings that still pass current policy.

    Legacy rows predate strict settings validation and may contain unknown or
    now-unsafe values. Serialization therefore reuses the same generic and
    connector-specific validators as writes, and fails closed without ever
    falling back to the raw JSON column.
    """
    try:
        connector = get_connector(connection.connector_type)
    except Exception:
        return {}

    applicable_names = tuple(
        field.name
        for field in connector.manifest.config_fields
        if not field.auth_types or connection.auth_type in field.auth_types
    )
    submitted = {
        name: connection.config_json[name]
        for name in applicable_names
        if name in connection.config_json
    }

    def validate(candidate: dict[str, object]) -> dict[str, object] | None:
        try:
            normalized = normalize_config(
                connector.manifest,
                connection.auth_type,
                candidate,
            )
            provider_validated = connector.validate_settings(
                connection.auth_type,
                normalized,
            )
            return {
                name: provider_validated[name]
                for name in applicable_names
                if name in provider_validated
            }
        except Exception:
            return None

    fully_validated = validate(submitted)
    if fully_validated is not None:
        return fully_validated

    # Preserve independently valid public settings when one legacy field is
    # stale. Revalidate the accumulated candidate on every addition so an
    # unsafe cross-field combination can never be serialized.
    accepted: dict[str, object] = {}
    safe: dict[str, object] = {}
    pending = [(name, submitted[name]) for name in applicable_names if name in submitted]
    while pending:
        progress = False
        remaining: list[tuple[str, object]] = []
        for name, value in pending:
            candidate = {**accepted, name: value}
            validated = validate(candidate)
            if validated is None:
                remaining.append((name, value))
                continue
            accepted = candidate
            safe = validated
            progress = True
        if not progress:
            break
        pending = remaining
    return safe


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
    if set(credentials) - declared:
        raise _bad_request("Unknown credential fields")
    missing = [name for name in scheme.required_field_names() if not credentials.get(name)]
    if missing:
        raise _bad_request(f"Missing required credential fields: {', '.join(missing)}")


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
    try:
        normalized_config = normalize_config(connector.manifest, auth_type, config)
        normalized_config = connector.validate_settings(auth_type, normalized_config)
    except ValueError as exc:
        raise _bad_request(str(exc)) from None

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
    if webhook_secret_mode(connector) == "generated":
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
        config_json=normalized_config,
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


async def set_webhook_secret(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    connection_id: UUID,
    *,
    secret: str,
    request_id: UUID,
    ip_hash: str,
) -> None:
    """Create or rotate a provider-supplied webhook secret without readback."""
    connection = await db.scalar(
        select(Connection)
        .where(
            Connection.id == connection_id,
            Connection.workspace_id == ctx.workspace_id,
        )
        .with_for_update()
    )
    if connection is None:
        raise _not_found()
    connector = get_connector(connection.connector_type)
    if webhook_secret_mode(connector) != "provider_supplied":
        raise _bad_request("Connector does not accept provider-supplied webhook secrets")

    store = SecretStore(db, crypto)
    action: str
    if connection.webhook_secret_id is None:
        stored = await store.create(
            workspace_id=ctx.workspace_id,
            name=f"connection/{connection.public_id}/webhook",
            plaintext=secret,
            secret_type=SecretType.WEBHOOK_SECRET,
            created_by_user_id=ctx.user.id,
        )
        connection.webhook_secret_id = stored.id
        action = "connection.webhook_secret_configured"
    else:
        await store.rotate(ctx.workspace_id, connection.webhook_secret_id, secret)
        action = "connection.webhook_secret_rotated"

    audit.record(
        db,
        action=action,
        target_type="connection",
        target_id=connection.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={},
    )
    await db.commit()


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
    credentials = _decode_stored_credentials(plaintext)
    try:
        provider_health = await connector.verify_connection(
            VerifyContext(
                auth_type=connection.auth_type,
                credentials=credentials,
                config=dict(connection.config_json),
            )
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Provider connection verification failed",
        ) from None
    try:
        safe_health = _sanitize_provider_document(
            {
                "message": provider_health.message,
                "details": provider_health.details,
            }
        )
        if not isinstance(safe_health, dict):
            raise _ProviderOutputLimitError("provider health has an invalid shape")
        health = ConnectionHealth(
            ok=provider_health.ok,
            message=safe_health["message"],
            details=safe_health["details"],
        )
    except (_ProviderOutputLimitError, KeyError, TypeError, ValueError):
        raise _unsafe_provider_output() from None
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


async def fetch_metadata(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    connection_id: UUID,
) -> dict[str, object]:
    """Connector-provided, display-safe metadata for UI pickers (plan 17.10)."""
    connection = await get_connection(db, ctx.workspace_id, connection_id)
    connector = get_connector(connection.connector_type)
    if connection.encrypted_secret_id is None:
        raise _bad_request("Connection has no stored credential")
    store = SecretStore(db, crypto)
    plaintext = await store.reveal(ctx.workspace_id, connection.encrypted_secret_id)
    credentials = _decode_stored_credentials(plaintext)
    try:
        provider_metadata = await connector.fetch_metadata(
            VerifyContext(
                auth_type=connection.auth_type,
                credentials=credentials,
                config=dict(connection.config_json),
            )
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Provider metadata fetch failed: {type(exc).__name__}",
        ) from None
    try:
        safe_metadata = _sanitize_provider_document(provider_metadata)
        if not isinstance(safe_metadata, dict):
            raise _ProviderOutputLimitError("provider metadata has an invalid shape")
    except _ProviderOutputLimitError:
        raise _unsafe_provider_output() from None
    return cast(dict[str, object], safe_metadata)


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
