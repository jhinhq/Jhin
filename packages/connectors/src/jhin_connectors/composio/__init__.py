"""Composio-managed authentication, with native tool execution retained."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx

from jhin_connectors.http_client import ProviderHTTPError, send_bounded_json
from jhin_db.models import Connection
from jhin_secrets.redaction import get_redactor

COMPOSIO_ISSUER = "https://composio.dev"
API_BASE_URL = "https://backend.composio.dev/api/v3.1"
NATIVE_TOOLKITS = {
    "github": "github",
    "linear": "linear",
    "vercel": "vercel",
    "supabase": "supabase",
}
_NATIVE_AUTH = {
    "github": "oauth",
    "linear": "oauth",
    "vercel": "access_token",
    "supabase": "management_token",
}
_ORIGINS = {
    "github": "https://api.github.com",
    "linear": "https://api.linear.app",
    "vercel": "https://api.vercel.com",
    "supabase": "https://api.supabase.com",
}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


class ComposioError(Exception):
    """A fixed display-safe error, never provider text or credentials."""

    def __init__(self, message: str, *, needs_reauth: bool = False) -> None:
        super().__init__(message)
        self.needs_reauth = needs_reauth


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ComposioError("Managed app identifier is invalid")
    return value


def managed_user_id(workspace_id: UUID, user_id: UUID) -> str:
    return f"jhin:{workspace_id}:{user_id}"


def native_auth_type(connector_type: str) -> str:
    if connector_type not in _NATIVE_AUTH:
        raise ComposioError("This app does not support managed native authentication")
    return _NATIVE_AUTH[connector_type]


def validate_native_target(connector_type: str, auth_type: str, config: dict[str, Any]) -> None:
    """Managed OAuth tokens may only be sent to the toolkit's official API."""
    if auth_type != native_auth_type(connector_type):
        raise ComposioError("Managed app authentication type does not match this connection")
    base_url = config.get("base_url") or _ORIGINS[connector_type]
    if not isinstance(base_url, str) or base_url.rstrip("/") != _ORIGINS[connector_type]:
        raise ComposioError("Managed app credentials require the official provider origin")


def configured_auth_configs() -> dict[str, str]:
    try:
        parsed = json.loads(os.environ.get("COMPOSIO_AUTH_CONFIGS", "{}"))
        if not isinstance(parsed, dict) or len(parsed) > 200:
            raise ValueError
        return {_identifier(key): _identifier(value) for key, value in parsed.items()}
    except (ValueError, TypeError, ComposioError):
        raise ComposioError("Managed app authentication configuration is invalid") from None


def is_managed_connection(connection: Connection) -> bool:
    return connection.oauth_issuer == COMPOSIO_ISSUER


@dataclass(frozen=True)
class ComposioLink:
    id: str
    redirect_url: str
    expires_at: datetime


class ComposioClient:
    """Fixed-origin, redirect-free broker client; supplied HTTP clients stay caller-owned."""

    def __init__(
        self, http_client: httpx.AsyncClient | None = None, *, api_key: str | None = None
    ) -> None:
        self._http = http_client
        self._key = api_key if api_key is not None else os.environ.get("COMPOSIO_API_KEY", "")
        if not self._key or not self._key.isascii() or any(char in self._key for char in "\r\n\0"):
            raise ComposioError("Managed app sign-in is not configured")
        get_redactor().register(self._key)

    async def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        async def send(http: httpx.AsyncClient) -> dict[str, Any]:
            request = http.build_request(
                method,
                API_BASE_URL + path,
                headers={"x-api-key": self._key},
                json=body,
                timeout=30.0,
            )
            try:
                result = await send_bounded_json(http, request, max_response_bytes=524_288)
            except ProviderHTTPError:
                raise ComposioError("Managed app service request failed") from None
            if not isinstance(result, dict):
                raise ComposioError("Managed app service returned an invalid response")
            return result

        if self._http is not None:
            return await send(self._http)
        async with httpx.AsyncClient(follow_redirects=False, timeout=30.0, trust_env=False) as http:
            return await send(http)

    async def create_auth_config(self, toolkit: str) -> dict[str, Any]:
        result = await self._request(
            "POST",
            "/auth_configs",
            {
                "toolkit": {"slug": _identifier(toolkit)},
                "auth_config": {
                    "type": "use_composio_managed_auth",
                    "credentials": {},
                    "restrict_to_following_tools": [],
                },
            },
        )
        config = result.get("auth_config", result)
        if not isinstance(config, dict):
            raise ComposioError("Managed app authentication configuration is invalid")
        _identifier(config.get("id"))
        return result

    async def get_auth_config(self, auth_config_id: str) -> dict[str, Any]:
        result = await self._request("GET", f"/auth_configs/{_identifier(auth_config_id)}")
        config = result.get("auth_config", result)
        if not isinstance(config, dict) or config.get("id") != auth_config_id:
            raise ComposioError("Managed app authentication configuration is invalid")
        return result

    async def ensure_auth_config(self, toolkit: str) -> dict[str, Any]:
        """Reuse an enabled managed OAuth config before provisioning a new one."""
        slug = _identifier(toolkit)
        query = {
            "toolkit_slug": slug,
            "is_composio_managed": "true",
            "show_disabled": "false",
            "limit": "50",
        }
        for _ in range(4):
            result = await self._request("GET", "/auth_configs?" + urlencode(query))
            items = result.get("items")
            if not isinstance(items, list) or len(items) > 50:
                raise ComposioError("Managed app authentication listing is invalid")
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_toolkit = item.get("toolkit")
                if (
                    isinstance(item_toolkit, dict)
                    and item_toolkit.get("slug") == slug
                    and item.get("status") == "ENABLED"
                    and item.get("is_composio_managed") is True
                    and item.get("auth_scheme") == "OAUTH2"
                ):
                    return await self.get_auth_config(_identifier(item.get("id")))
            cursor = result.get("next_cursor")
            if not cursor:
                return await self.create_auth_config(slug)
            if not isinstance(cursor, str) or len(cursor) > 2000:
                raise ComposioError("Managed app authentication listing is invalid")
            query["cursor"] = cursor
        raise ComposioError(
            "Managed app has too many authentication configurations; configure its ID"
        )

    async def create_link(
        self, auth_config_id: str, user_id: str, callback_url: str
    ) -> ComposioLink:
        result = await self._request(
            "POST",
            "/connected_accounts/link",
            {
                "auth_config_id": _identifier(auth_config_id),
                "user_id": user_id,
                "callback_url": callback_url,
            },
        )
        identifier = _identifier(result.get("connected_account_id"))
        redirect = result.get("redirect_url")
        expires = result.get("expires_at")
        if not isinstance(redirect, str) or len(redirect) > 4096 or not isinstance(expires, str):
            raise ComposioError("Managed app sign-in response is invalid")
        parsed = urlsplit(redirect)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or not (host == "composio.dev" or host.endswith(".composio.dev"))
        ):
            raise ComposioError("Managed app sign-in destination is invalid")
        try:
            expiration = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if expiration.tzinfo is None:
                raise ValueError
        except ValueError:
            raise ComposioError("Managed app sign-in expiry is invalid") from None
        return ComposioLink(identifier, redirect, expiration)

    async def get_account(self, account_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/connected_accounts/{_identifier(account_id)}")

    async def delete_account(self, account_id: str) -> None:
        await self._request("DELETE", f"/connected_accounts/{_identifier(account_id)}")

    async def revoke_account(self, account_id: str) -> None:
        await self._request("POST", f"/connected_accounts/{_identifier(account_id)}/revoke")

    async def complete_auth(self, *, session_uri: str, user_id: str) -> dict[str, str]:
        """Redeem the opaque verifier session without ever dereferencing its URI."""
        if (
            not session_uri
            or len(session_uri) > 4096
            or any(char in session_uri for char in "\r\n\0")
        ):
            raise ComposioError("Managed app verification session is invalid")
        if not user_id or len(user_id) > 200 or any(char in user_id for char in "\r\n\0"):
            raise ComposioError("Managed app verification identity is invalid")
        get_redactor().register(session_uri)
        result = await self._request(
            "POST",
            "/connected_accounts/complete_auth",
            {"session_uri": session_uri, "user_id": user_id},
        )
        return {
            "connected_account_id": _identifier(result.get("connected_account_id")),
            "toolkit_slug": _identifier(result.get("toolkit_slug")),
        }


def validate_account(
    account: dict[str, Any], *, account_id: str, user_id: str, toolkit: str, auth_config_id: str
) -> None:
    if (
        account.get("id") != account_id
        or account.get("user_id") != user_id
        or not isinstance(account.get("toolkit"), dict)
        or account["toolkit"].get("slug") != toolkit
        or not isinstance(account.get("auth_config"), dict)
        or account["auth_config"].get("id") != auth_config_id
    ):
        raise ComposioError("Managed app account does not match this connection")
    if (
        account.get("status") != "ACTIVE"
        or account.get("is_disabled") is True
        or account["auth_config"].get("is_disabled") is True
    ):
        raise ComposioError("This managed app needs to be reconnected", needs_reauth=True)


async def resolve_managed_credentials(
    connection: Connection, credentials: dict[str, str], *, client: ComposioClient | None = None
) -> dict[str, str]:
    """Resolve the encrypted broker binding to a native token for this call only."""
    if not is_managed_connection(connection):
        return credentials
    connector = connection.connector_type
    if connector == "composio" and connection.auth_type == "managed":
        user = connection.oauth_authorized_by_user_id
        if (
            user is None
            or credentials.get("composio_user_id") != managed_user_id(connection.workspace_id, user)
            or credentials.get("composio_toolkit") != connection.config_json.get("toolkit")
        ):
            raise ComposioError("Managed app binding does not match this connection")
        _identifier(credentials.get("composio_account_id"))
        _identifier(credentials.get("composio_auth_config_id"))
        _identifier(credentials.get("composio_toolkit"))
        # The community connector validates this binding against the remote
        # account before verification/discovery/execution; no native token.
        return credentials
    validate_native_target(connector, connection.auth_type, connection.config_json)
    user = connection.oauth_authorized_by_user_id
    if user is None:
        raise ComposioError("Managed app connection has no authorizing user")
    expected_user = managed_user_id(connection.workspace_id, user)
    if (
        credentials.get("composio_user_id") != expected_user
        or credentials.get("composio_toolkit") != NATIVE_TOOLKITS[connector]
    ):
        raise ComposioError("Managed app binding does not match this connection")
    account_id = _identifier(credentials.get("composio_account_id"))
    config_id = _identifier(credentials.get("composio_auth_config_id"))
    account = await (client or ComposioClient()).get_account(account_id)
    validate_account(
        account,
        account_id=account_id,
        user_id=expected_user,
        toolkit=NATIVE_TOOLKITS[connector],
        auth_config_id=config_id,
    )
    state = account.get("state")
    token_data = state.get("val") if isinstance(state, dict) else None
    if not isinstance(token_data, dict):
        token_data = account.get("data")
    token = token_data.get("access_token") if isinstance(token_data, dict) else None
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 32768
        or any(char in token for char in "\r\n\0")
    ):
        raise ComposioError("Managed app account has no usable access token", needs_reauth=True)
    get_redactor().register(token)
    return {"token" if connector == "vercel" else "access_token": token}
