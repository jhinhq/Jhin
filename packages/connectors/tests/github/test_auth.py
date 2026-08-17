"""GitHub auth: PAT header material, app JWT claims, and installation-token
caching with a fake clock (no network)."""

import asyncio
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from jhin_connectors.github.auth import (
    GitHubAuthError,
    InstallationTokenCache,
    _CachedToken,
    build_app_jwt,
    resolve_access_token,
)
from jhin_connectors.github.client import github_headers


def _test_private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


async def test_pat_token_used_directly() -> None:
    token = await resolve_access_token("pat", {"token": "ghp_abc123456"}, "https://api.github.com")
    assert token == "ghp_abc123456"


async def test_pat_missing_token_fails() -> None:
    with pytest.raises(GitHubAuthError, match="missing token"):
        await resolve_access_token("pat", {}, "https://api.github.com")


async def test_unsupported_auth_type_fails() -> None:
    with pytest.raises(GitHubAuthError, match="unsupported"):
        await resolve_access_token("oauth", {}, "https://api.github.com")


def test_bearer_header_and_pinned_api_version() -> None:
    headers = github_headers("tok-123")
    assert headers["Authorization"] == "Bearer tok-123"
    assert headers["X-GitHub-Api-Version"] == "2026-03-10"
    assert headers["Accept"] == "application/vnd.github+json"


def test_app_jwt_claims_match_github_requirements() -> None:
    pem = _test_private_key()
    now = 1_800_000_000
    token = build_app_jwt(
        {"app_id": "Iv23abcDEF", "private_key": pem, "installation_id": "42"}, now=now
    )
    claims = pyjwt.decode(token, options={"verify_signature": False}, algorithms=["RS256"])
    assert claims["iss"] == "Iv23abcDEF"
    assert claims["iat"] == now - 60  # clock-drift buffer
    assert claims["exp"] == now + 540  # under GitHub's 10-minute maximum
    assert pyjwt.get_unverified_header(token)["alg"] == "RS256"


def test_app_jwt_missing_fields_fail_without_leaking_material() -> None:
    with pytest.raises(GitHubAuthError, match="missing app_id"):
        build_app_jwt({"private_key": "x"})
    with pytest.raises(GitHubAuthError, match="missing private_key"):
        build_app_jwt({"app_id": "1"})
    with pytest.raises(GitHubAuthError) as excinfo:
        build_app_jwt({"app_id": "1", "private_key": "not-a-pem-secret"})
    assert "not-a-pem-secret" not in str(excinfo.value)


async def test_installation_token_cache_reuses_until_near_expiry() -> None:
    clock = {"now": 1000.0}
    mints = 0

    async def mint() -> _CachedToken:
        nonlocal mints
        mints += 1
        return _CachedToken(token=f"ghs_token_{mints}", expires_at=clock["now"] + 3600)

    cache = InstallationTokenCache(clock=lambda: clock["now"])
    assert await cache.get("key", mint) == "ghs_token_1"
    assert await cache.get("key", mint) == "ghs_token_1"  # cached
    clock["now"] += 3000  # still > 2 min from expiry
    assert await cache.get("key", mint) == "ghs_token_1"
    clock["now"] += 500  # within the 120s refresh margin
    assert await cache.get("key", mint) == "ghs_token_2"
    assert mints == 2


async def test_installation_token_cache_is_per_key() -> None:
    async def mint_a() -> _CachedToken:
        return _CachedToken(token="ghs_a", expires_at=time.time() + 3600)

    async def mint_b() -> _CachedToken:
        return _CachedToken(token="ghs_b", expires_at=time.time() + 3600)

    cache = InstallationTokenCache()
    a, b = await asyncio.gather(cache.get("a", mint_a), cache.get("b", mint_b))
    assert (a, b) == ("ghs_a", "ghs_b")
