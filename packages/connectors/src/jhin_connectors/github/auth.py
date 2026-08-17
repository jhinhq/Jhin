"""GitHub authentication (plan 11.2, 13.6).

Two schemes:

- **PAT** (``pat``): the stored token is used directly. Simple self-hosted
  path.
- **GitHub App** (``github_app``): preferred for production — scopeable and
  revocable. The stored app credential (app/client id + RS256 private key +
  installation id) is exchanged for a short-lived installation token via
  ``POST /app/installations/{id}/access_tokens`` (expires after one hour;
  docs.github.com, API version 2026-03-10). Tokens are cached in process
  memory keyed by a credential fingerprint, refreshed with a safety margin,
  and treated as opaque strings (GitHub's 2026 stateless ``ghs_`` format can
  exceed 500 characters).

Every minted/loaded token is registered with the process secret redactor so
it can never survive into persisted output or logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import jwt

from jhin_connectors.github.client import API_VERSION, GitHubApiError, error_from_response
from jhin_secrets.redaction import get_redactor

AUTH_PAT = "pat"
AUTH_GITHUB_APP = "github_app"

# App JWTs: issued 60s in the past for clock drift, valid 9 minutes
# (10 minutes is GitHub's hard maximum).
_JWT_DRIFT_SECONDS = 60
_JWT_TTL_SECONDS = 9 * 60

# Refresh installation tokens two minutes before GitHub's one-hour expiry.
_TOKEN_REFRESH_MARGIN_SECONDS = 120


class GitHubAuthError(Exception):
    """Credential material is missing or the token exchange failed. Messages
    are safe for models/users — never key or token material."""


def build_app_jwt(credentials: dict[str, str], *, now: float | None = None) -> str:
    """RS256 app JWT per GitHub's requirements (iss = client id or app id)."""
    issuer = credentials.get("app_id", "").strip()
    private_key = credentials.get("private_key", "")
    if not issuer:
        raise GitHubAuthError("GitHub App credential is missing app_id")
    if not private_key:
        raise GitHubAuthError("GitHub App credential is missing private_key")
    issued_at = int(now if now is not None else time.time())
    try:
        return jwt.encode(
            {
                "iat": issued_at - _JWT_DRIFT_SECONDS,
                "exp": issued_at + _JWT_TTL_SECONDS,
                "iss": issuer,
            },
            private_key,
            algorithm="RS256",
        )
    except Exception as exc:  # invalid PEM and friends — keep material out
        raise GitHubAuthError(f"cannot sign GitHub App JWT: {type(exc).__name__}") from None


@dataclass(frozen=True)
class _CachedToken:
    token: str
    expires_at: float  # unix seconds


def _credential_fingerprint(base_url: str, credentials: dict[str, str]) -> str:
    """Cache key that changes whenever any credential field rotates."""
    material = "|".join(
        (
            base_url,
            credentials.get("app_id", ""),
            credentials.get("installation_id", ""),
            credentials.get("private_key", ""),
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


class InstallationTokenCache:
    """Process-wide cache of short-lived installation tokens.

    ``clock`` is injectable for tests. One asyncio lock serializes minting so
    concurrent tool calls do not stampede the token endpoint.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._tokens: dict[str, _CachedToken] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str, mint: Callable[[], Awaitable[_CachedToken]]) -> str:
        now = self._clock()
        cached = self._tokens.get(key)
        if cached is not None and cached.expires_at - _TOKEN_REFRESH_MARGIN_SECONDS > now:
            return cached.token
        async with self._lock:
            cached = self._tokens.get(key)
            now = self._clock()
            if cached is not None and cached.expires_at - _TOKEN_REFRESH_MARGIN_SECONDS > now:
                return cached.token
            fresh = await mint()
            self._tokens[key] = fresh
            return fresh.token


_installation_tokens = InstallationTokenCache()


def _parse_expires_at(raw: str) -> float:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        # Defensive: assume the documented one hour if the field is odd.
        return datetime.now(UTC).timestamp() + 3600


async def mint_installation_token(
    base_url: str, credentials: dict[str, str], *, now: float | None = None
) -> _CachedToken:
    """One uncached exchange: app JWT -> installation access token."""
    installation_id = credentials.get("installation_id", "").strip()
    if not installation_id:
        raise GitHubAuthError("GitHub App credential is missing installation_id")
    app_jwt = build_app_jwt(credentials, now=now)
    url = f"{base_url.rstrip('/')}/app/installations/{installation_id}/access_tokens"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {app_jwt}",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
    if response.status_code != 201:
        raise error_from_response("POST", "/app/installations/.../access_tokens", response)
    payload = response.json()
    token = str(payload.get("token", ""))
    if not token:
        raise GitHubAuthError("token exchange response contained no token")
    get_redactor().register(token)
    return _CachedToken(token=token, expires_at=_parse_expires_at(str(payload.get("expires_at"))))


async def resolve_access_token(
    auth_type: str,
    credentials: dict[str, str],
    base_url: str,
    *,
    cache: InstallationTokenCache | None = None,
) -> str:
    """The bearer token to use for one API call, minted/cached as needed."""
    if auth_type == AUTH_PAT:
        token = credentials.get("token", "")
        if not token:
            raise GitHubAuthError("PAT credential is missing token")
        get_redactor().register(token)
        return token
    if auth_type == AUTH_GITHUB_APP:
        active_cache = cache if cache is not None else _installation_tokens
        key = _credential_fingerprint(base_url, credentials)
        return await active_cache.get(key, lambda: mint_installation_token(base_url, credentials))
    raise GitHubAuthError(f"unsupported GitHub auth type: {auth_type!r}")


__all__ = [
    "AUTH_GITHUB_APP",
    "AUTH_PAT",
    "GitHubApiError",
    "GitHubAuthError",
    "InstallationTokenCache",
    "build_app_jwt",
    "mint_installation_token",
    "resolve_access_token",
]
