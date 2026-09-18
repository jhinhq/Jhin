"""Composio hosted sign-in with Jhin-owned state and account bindings."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.connections import service as connections
from jhin_api.deps import WorkspaceContext
from jhin_api.oauth import redirect
from jhin_api.oauth.schemas import OAuthProbeOut, OAuthStartIn, OAuthStartOut, ProbeFlow
from jhin_api.settings import Settings
from jhin_connectors import normalize_config
from jhin_connectors.composio import (
    COMPOSIO_ISSUER,
    NATIVE_TOOLKITS,
    ComposioClient,
    ComposioError,
    managed_user_id,
    native_auth_type,
    resolve_managed_credentials,
    validate_account,
    validate_native_target,
)
from jhin_db.models import Connection
from jhin_db.models.connection import new_public_id
from jhin_domain import ConnectionStatus, SecretType
from jhin_oauth.persistence import PendingAuthorizationInvalid, PendingAuthorizationStore
from jhin_secrets import SecretCrypto, SecretStore


def configured(settings: Settings) -> bool:
    return bool(settings.composio_api_key.get_secret_value().strip())


def callback_url(settings: Settings) -> str:
    return f"{redirect.redirect_base(settings)}/api/v1/oauth/composio/callback"


CALLBACK_COOKIE = "jhin_composio_state"
CALLBACK_PATH = "/api/v1/oauth/composio"


def set_callback_cookie(response: Response, result: OAuthStartOut, settings: Settings) -> None:
    if result._composio_state:
        response.set_cookie(
            CALLBACK_COOKIE,
            result._composio_state,
            max_age=min(settings.oauth_state_ttl_seconds, 600),
            secure=redirect.is_https_redirect(settings),
            httponly=True,
            samesite="lax",
            path=CALLBACK_PATH,
        )


def connector_info(connector_type: str, settings: Settings) -> dict[str, Any] | None:
    if connector_type == "composio":
        return {
            "provider": "composio",
            "configured": configured(settings),
            "toolkit": "",
            "auth_type": "managed",
        }
    if connector_type not in NATIVE_TOOLKITS:
        return None
    return {
        "provider": "composio",
        "configured": configured(settings)
        and (connector_type != "vercel" or bool(settings.composio_auth_configs.get("vercel"))),
        "toolkit": NATIVE_TOOLKITS[connector_type],
        "auth_type": native_auth_type(connector_type),
    }


def probe(settings: Settings, connector_type: str = "") -> OAuthProbeOut:
    ready = configured(settings) and (
        connector_type != "vercel" or bool(settings.composio_auth_configs.get("vercel"))
    )
    return OAuthProbeOut(
        method="composio",
        supports_oauth=True,
        supports_dcr=False,
        issuer=COMPOSIO_ISSUER,
        authorization_server_display="composio.dev",
        client_configured=ready,
        reason=""
        if ready
        else (
            "composio_auth_config_required" if configured(settings) else "composio_not_configured"
        ),
        redirect_flow=ProbeFlow(available=ready),
    )


def _client(settings: Settings, http_client: httpx.AsyncClient) -> ComposioClient:
    return ComposioClient(
        http_client=http_client, api_key=settings.composio_api_key.get_secret_value()
    )


async def start(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    http_client: httpx.AsyncClient,
    settings: Settings,
    payload: OAuthStartIn,
    *,
    request_id: UUID,
    ip_hash: str,
) -> OAuthStartOut:
    if not configured(settings):
        raise HTTPException(
            409,
            "An administrator must configure the Composio project API key before connecting apps.",
        )
    generic = payload.connector_type == "composio"
    toolkit = (
        payload.config.get("toolkit") if generic else NATIVE_TOOLKITS.get(payload.connector_type)
    )
    if toolkit is None:
        raise HTTPException(400, "This app does not have a managed connection configured.")
    connector = connections.get_connector(payload.connector_type)
    auth_type = "managed" if generic else native_auth_type(payload.connector_type)
    target = None
    if payload.connection_id is not None:
        target = await connections.get_connection(db, ctx.workspace_id, payload.connection_id)
        if target.connector_type != payload.connector_type:
            raise HTTPException(400, "The app does not match this connection.")
        if target.auth_type == "postgres":
            raise HTTPException(400, "PostgreSQL connections use a dedicated database login.")
        if generic and target.config_json.get("toolkit") != toolkit:
            raise HTTPException(400, "The app does not match this connection.")
    try:
        config = normalize_config(connector.manifest, auth_type, payload.config)
        config = connector.validate_settings(auth_type, config)
        if not generic:
            validate_native_target(payload.connector_type, auth_type, config)
    except (ValueError, ComposioError) as exc:
        raise HTTPException(422, str(exc)) from None
    await connections.ensure_server_slug_is_free(
        db,
        ctx.workspace_id,
        payload.connector_type,
        config,
        exclude_id=target.id if target else None,
    )
    client = _client(settings, http_client)
    if payload.connector_type == "vercel" and not settings.composio_auth_configs.get("vercel"):
        raise HTTPException(
            409,
            "Configure a Vercel OAuth auth config in Composio and add its ID "
            "to COMPOSIO_AUTH_CONFIGS.",
        )
    try:
        config_id = settings.composio_auth_configs.get(toolkit)
        auth_config = (
            await client.get_auth_config(config_id)
            if config_id
            else await client.ensure_auth_config(toolkit)
        )
        auth = auth_config.get("auth_config", auth_config)
        config_id = auth.get("id")
        if (
            not isinstance(config_id, str)
            or not config_id
            or auth_config.get("toolkit", {}).get("slug") != toolkit
            or auth.get("is_disabled") is True
            or auth.get("status") == "DISABLED"
            or auth.get("auth_scheme") != "OAUTH2"
        ):
            raise ValueError("invalid managed auth configuration")
        pending = PendingAuthorizationStore(db, crypto)
        row, state = await pending.create(
            workspace_id=ctx.workspace_id,
            user_id=ctx.user.id,
            flow="authorization_code",
            connector_type=payload.connector_type,
            ttl_seconds=min(settings.oauth_state_ttl_seconds, 600),
            connection_id=target.id if target else None,
            issuer=COMPOSIO_ISSUER,
            redirect_uri=callback_url(settings),
            draft={"name": payload.name, "config": config},
        )
        external_user = managed_user_id(ctx.workspace_id, ctx.user.id)
        link = await client.create_link(
            auth_config_id=config_id,
            user_id=external_user,
            callback_url=f"{row.redirect_uri}?{urlencode({'state': state})}",
        )
        binding = {
            "composio_account_id": link.id,
            "composio_user_id": external_user,
            "composio_toolkit": toolkit,
            "composio_auth_config_id": config_id,
        }
        row.draft_json = {**row.draft_json, "binding": binding}
        if link.expires_at < row.expires_at:
            row.expires_at = link.expires_at
            row.retain_until = link.expires_at
        audit.record(
            db,
            action="connection.oauth_started",
            target_type="connection",
            target_id=target.id if target else None,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user.id,
            request_id=request_id,
            ip_hash=ip_hash,
            metadata={"connector_type": payload.connector_type, "registration_source": "composio"},
        )
        await db.commit()
    except HTTPException:
        raise
    except Exception:
        await db.rollback()
        raise HTTPException(
            502,
            "Composio could not start sign-in. Check the managed app configuration and try again.",
        ) from None
    result = OAuthStartOut(
        authorization_url=link.redirect_url,
        state_expires_at=row.expires_at,
        issuer=COMPOSIO_ISSUER,
        scopes=[],
        resource="",
        authorized_as_user_id=ctx.user.id,
        client_source="composio",
    )
    result._composio_state = state
    return result


async def complete(
    db: AsyncSession,
    crypto: SecretCrypto,
    http_client: httpx.AsyncClient,
    settings: Settings,
    *,
    user_id: UUID,
    state: str,
    session_uri: str,
    request_id: UUID,
    ip_hash: str,
) -> Any:
    from jhin_api.oauth.service import CallbackResult, _still_admin, receipt_ttl

    pending = PendingAuthorizationStore(db, crypto)
    try:
        preview = await pending.peek(
            handle=state, expected_user_id=user_id, expected_flow="authorization_code"
        )
        if preview.issuer != COMPOSIO_ISSUER:
            return CallbackResult(connection=None, error="expired")
        if not await _still_admin(db, user_id=user_id, workspace_id=preview.workspace_id):
            return CallbackResult(connection=None, error="expired")
        # Composio holds the grant pending until the same browser returns.
        # Looking up an ACTIVE account alone permits shared-link session fixation.
        verified = await _client(settings, http_client).complete_auth(
            session_uri=session_uri, user_id=managed_user_id(preview.workspace_id, user_id)
        )
        if (
            verified.get("connected_account_id")
            != preview.draft_json["binding"]["composio_account_id"]
            or verified.get("toolkit_slug") != preview.draft_json["binding"]["composio_toolkit"]
        ):
            return CallbackResult(connection=None, error="failed")
        row = await pending.claim(
            handle=state, expected_user_id=user_id, expected_flow="authorization_code"
        )
    except PendingAuthorizationInvalid:
        await db.rollback()
        receipt = await pending.recall(
            handle=state, expected_user_id=user_id, expected_flow="authorization_code"
        )
        if (
            receipt is not None
            and receipt.issuer == COMPOSIO_ISSUER
            and receipt.outcome == "connected"
            and await _still_admin(db, user_id=user_id, workspace_id=receipt.workspace_id)
        ):
            connection = (
                await db.get(Connection, receipt.outcome_connection_id)
                if receipt.outcome_connection_id
                else None
            )
            if connection is not None and connection.workspace_id == receipt.workspace_id:
                return CallbackResult(
                    connection=connection,
                    error=None,
                    public_id=connection.public_id,
                    connector_type=connection.connector_type,
                )
        return CallbackResult(connection=None, error="expired")
    connector_type = row.connector_type
    row_id = row.id
    target_public_id = None
    if row.connection_id is not None:
        target = await connections.get_connection(db, row.workspace_id, row.connection_id)
        target_public_id = target.public_id
    try:
        if row.redirect_uri != callback_url(settings) or not await _still_admin(
            db, user_id=user_id, workspace_id=row.workspace_id
        ):
            raise ValueError("callback context changed")
        binding = row.draft_json["binding"]
        generic = connector_type == "composio"
        toolkit = (
            row.draft_json["config"].get("toolkit") if generic else NATIVE_TOOLKITS[connector_type]
        )
        if (
            binding["composio_user_id"] != managed_user_id(row.workspace_id, user_id)
            or binding["composio_toolkit"] != toolkit
        ):
            raise ValueError("binding mismatch")
        account = await _client(settings, http_client).get_account(binding["composio_account_id"])
        validate_account(
            account,
            account_id=binding["composio_account_id"],
            user_id=binding["composio_user_id"],
            toolkit=toolkit,
            auth_config_id=binding["composio_auth_config_id"],
        )
        connector = connections.get_connector(connector_type)
        auth_type = "managed" if generic else native_auth_type(connector_type)
        config = connector.validate_settings(
            auth_type, normalize_config(connector.manifest, auth_type, row.draft_json["config"])
        )
        await connections.ensure_server_slug_is_free(
            db, row.workspace_id, connector_type, config, exclude_id=row.connection_id
        )
        if row.connection_id is not None:
            connection = await connections.get_connection(db, row.workspace_id, row.connection_id)
            if connection.connector_type != connector_type or connection.auth_type == "postgres":
                raise ValueError("reconnect target changed")
        else:
            connection = Connection(
                workspace_id=row.workspace_id,
                connector_type=connector_type,
                name=row.draft_json["name"],
                auth_type=auth_type,
                public_id=new_public_id(),
                created_by_user_id=user_id,
            )
            db.add(connection)
        store = SecretStore(db, crypto)
        old_account_id = None
        if connection.oauth_issuer == COMPOSIO_ISSUER and connection.encrypted_secret_id:
            old_binding = connections._decode_stored_credentials(
                await store.reveal(row.workspace_id, connection.encrypted_secret_id)
            )
            old_account_id = old_binding.get("composio_account_id")
        if connection.encrypted_secret_id:
            stored = await store.get(row.workspace_id, connection.encrypted_secret_id)
            stored.type = SecretType.COMPOSIO_BINDING.value
            await store.rotate(
                row.workspace_id, connection.encrypted_secret_id, json.dumps(binding)
            )
        else:
            secret = await store.create(
                workspace_id=row.workspace_id,
                name=f"connection/{connection.public_id}/credentials",
                plaintext=json.dumps(binding),
                secret_type=SecretType.COMPOSIO_BINDING,
                created_by_user_id=user_id,
            )
            connection.encrypted_secret_id = secret.id
        connection.auth_type = auth_type
        kept = {
            key: value
            for key, value in (connection.config_json or {}).items()
            if key in {"composio_tools", "composio_discovered_at", "tool_risk_overrides"}
        }
        connection.config_json = {**kept, **config}
        connection.oauth_issuer = COMPOSIO_ISSUER
        connection.oauth_authorized_by_user_id = user_id
        connection.oauth_client_registration_id = None
        connection.oauth_expires_at = None
        connection.oauth_refresh_expires_at = None
        connection.oauth_resource = None
        connection.oauth_scope = None
        connection.oauth_refresh_failures = 0
        if connection.status != ConnectionStatus.DISABLED.value:
            connection.status = ConnectionStatus.ACTIVE.value
        connection.last_error = None
        await resolve_managed_credentials(
            connection, binding, client=_client(settings, http_client)
        )
        if generic:
            from jhin_connectors.composio.tools import discovery_payload
            from jhin_connectors.composio.tools_client import ComposioToolsClient

            tools_client = ComposioToolsClient(
                http_client=http_client, api_key=settings.composio_api_key.get_secret_value()
            )
            discovered = discovery_payload(
                await tools_client.list_tools(
                    toolkit, version=config.get("toolkit_version", "latest")
                )
            )
            connection.config_json = {**connection.config_json, **discovered}
        # The active account, owner, toolkit and usable credential have all
        # been checked above; generic connections have also discovered tools.
        connection.last_verified_at = datetime.now(UTC)
        await db.flush()
        audit.record(
            db,
            action="connection.oauth_authorized",
            target_type="connection",
            target_id=connection.id,
            workspace_id=row.workspace_id,
            actor_id=user_id,
            request_id=request_id,
            ip_hash=ip_hash,
            metadata={"connector_type": connector_type, "registration_source": "composio"},
        )
        await pending.settle(
            row,
            outcome="connected",
            connection_id=connection.id,
            receipt_ttl_seconds=receipt_ttl(settings),
        )
        await db.commit()
        if old_account_id and old_account_id != binding["composio_account_id"]:
            # The new and old tokens can belong to the same provider grant.
            # Remove the superseded broker record without revoking that grant.
            with contextlib.suppress(Exception):
                await _client(settings, http_client).delete_account(old_account_id)
        return CallbackResult(
            connection=connection,
            error=None,
            public_id=connection.public_id,
            connector_type=connector_type,
        )
    except Exception:
        with contextlib.suppress(Exception):
            await db.rollback()
        from jhin_db.models import OAuthAuthorization

        failed = await db.get(OAuthAuthorization, row_id)
        if failed is not None:
            failed.consumed_at = datetime.now(UTC)
            await pending.settle(
                failed,
                outcome="failed",
                connection_id=failed.connection_id,
                receipt_ttl_seconds=receipt_ttl(settings),
            )
            await db.commit()
        return CallbackResult(
            connection=None,
            error="failed",
            connector_type=connector_type,
            public_id=target_public_id,
        )
